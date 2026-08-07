"""E2E integration tests for ChunkingService (stage 3A).

需要真实 PostgreSQL（127.0.0.1:5433）。真实 LocalRawArtifactStore + 真实
SourceParsingService（上游已收口）+ 真实 ChunkingService，零真实网络。
覆盖：
- HTML / PDF Source → ParsedSource → ChunkSet → DocumentChunks 首建
  （replayed=False、chunk_count 正确、fingerprint 64 hex）；
- 每个 Chunk 可完整回溯：chunk → chunk_set → ParsedSource → SourceRecord →
  RawArtifact，且 locator_refs（block_ordinal + char_start/char_end + 原
  locator）拼回 chunk.text == 各原 block 文本片段按 "\n" 连接；
- HTML DOM / PDF page locator 保留；
- SourceRecord / ParsedSource 在 chunking 后零修改；
- replay：同 ParsedSource + 同 chunker version → 复用原 ChunkSet，不重复插
  chunks；
- chunker version 变化 → 新 ChunkSet，旧版本保留；
- 并发相同 chunking → 只 1 个 ChunkSet + 一套 chunks；
- 完整性损坏（chunk text 被篡改 / chunk_count 不一致）→
  ChunkSetIntegrityError，**不自动修复**；
- ParsedSource 不存在 → ParsedSourceNotFound。
本阶段不创建 Chroma collection（源码级 guard 在单元测试侧）。
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update

from app.chunking.errors import ChunkSetIntegrityError, ParsedSourceNotFound
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.chunk_set import ChunkSetModel
from app.db.models.company import CompanyModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.repositories.chunk_set_repository import ChunkSetRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.parsed_source_block_repository import ParsedSourceBlockRepository
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.pdf_fixtures import single_page_pdf

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"
_SSE_URL = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# 简短 HTML：heading + 2 段落 → 3 blocks，全合并为 1 chunk。
_HTML = (
    "<html><head><title>标题</title></head><body><article>"
    "<h1>标题</h1>"
    "<p>第一段正文。</p>"
    "<p>第二段正文。</p>"
    "</article></body></html>"
).encode()

# 多段长 HTML：5 段各 200 字（文本互不相同，避开 html_dom v2 冻结的
# 相邻相同 block 去重）→ 每段 < target；段1+段2=401（结算），
# 段3+段4=401（结算），段5=200 → 3 chunks（[401, 401, 200]），refs [2, 2, 1]。
_MULTI_HTML = (
    "<html><head><title>多段文档</title></head><body><article>"
    + "".join(f"<p>{'甲乙丙丁戊'[i % 5] * 200}</p>" for i in range(5))
    + "</article></body></html>"
).encode()


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM document_chunks"))
        await session.execute(text("DELETE FROM chunk_sets"))
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM news_source_verifications"))
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code="600519",
                identity_key="SSE:600519",
                board="sse_main",
                official_name="测试公司",
                short_name="测试",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "raw_root": raw_root,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_html_source(env: dict, *, html: bytes = _HTML, source_url: str = _XINHUA_URL):
    stored = env["raw_store"].put_html_bytes(html)
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=env["company_id"],
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=_PUBLISHED_AT,
            source_url=source_url,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        return record.source_id, artifact.artifact_id, stored.storage_key


async def _seed_pdf_source(
    env: dict, *, pdf: bytes = single_page_pdf(title="季度报告"), source_url: str = _SSE_URL
):
    stored = env["raw_store"].put_pdf_stream(BytesIO(pdf))
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=env["company_id"],
            provider_key="sse",
            artifact_id=artifact.artifact_id,
            document_type="annual_report",
            title=_SOURCE_TITLE,
            published_at=_PUBLISHED_AT,
            source_url=source_url,
            acquisition_method="user_upload",
            status="available",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=["company_announcement", "document_download"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        return record.source_id, artifact.artifact_id, stored.storage_key


def _parse_service(env: dict) -> SourceParsingService:
    return SourceParsingService(env["sessionmaker"], env["raw_store"])


def _chunk_service(env: dict) -> ChunkingService:
    return ChunkingService(env["sessionmaker"])


async def _parse(env: dict, source_id) -> ParsedSourceModel:
    result = await _parse_service(env).parse_source(source_id)
    async with env["sessionmaker"]() as session:
        return await ParsedSourceRepository(session).get_by_id(result.parsed_source_id)


async def _blocks_by_ordinal(env: dict, parsed_source_id):
    async with env["sessionmaker"]() as session:
        blocks = await ParsedSourceBlockRepository(session).list_for_parsed_source(parsed_source_id)
    return {b.ordinal: b for b in blocks}


async def _chunks(env: dict, chunk_set_id):
    async with env["sessionmaker"]() as session:
        return await DocumentChunkRepository(session).list_for_chunk_set(chunk_set_id)


async def _counts(env: dict) -> tuple[int, int]:
    async with env["sessionmaker"]() as session:
        sets = (await session.execute(select(func.count(ChunkSetModel.chunk_set_id)))).scalar_one()
        chunks = (
            await session.execute(select(func.count(DocumentChunkModel.chunk_id)))
        ).scalar_one()
    return int(sets), int(chunks)


async def _verify_chunk_trace(env: dict, chunk, parsed_source_id, blocks_by_ordinal) -> None:
    """Chunk → ChunkSet → ParsedSource → SourceRecord → RawArtifact 完整回溯。"""
    async with env["sessionmaker"]() as session:
        chunk_set = await session.get(ChunkSetModel, chunk.chunk_set_id)
        assert chunk_set is not None
        assert chunk_set.parsed_source_id == parsed_source_id
        parsed = await ParsedSourceRepository(session).get_by_id(chunk_set.parsed_source_id)
        assert parsed is not None and parsed.parsed_source_id == parsed_source_id
        source = await SourceRecordRepository(session).get_by_id(parsed.source_id)
        assert source is not None
        artifact = await RawArtifactRepository(session).get_by_id(source.artifact_id)
        assert artifact is not None
        assert artifact.content_sha256 == parsed.raw_content_sha256
    # refs 片段按 block_ordinal 顺序以 "\n" 连接 == chunk.text。
    # （locator_refs 从 DB 读出是 JSONB list[dict]）
    segments = []
    for ref in chunk.locator_refs:
        block = blocks_by_ordinal[ref["block_ordinal"]]
        assert ref["locator"] == block.locator
        assert 0 <= ref["char_start"] < ref["char_end"] <= len(block.text)
        segments.append(block.text[ref["char_start"] : ref["char_end"]])
    assert "\n".join(segments) == chunk.text


# ---------------------------------------------------------------- HTML 首建


async def test_html_parsed_source_chunked_and_traceable(env) -> None:
    source_id, _, _ = await _seed_html_source(env)
    parsed = await _parse(env, source_id)
    result = await _chunk_service(env).chunk_parsed_source(parsed.parsed_source_id)

    assert result.replayed is False
    assert result.parsed_source_id == parsed.parsed_source_id
    assert result.chunker_name == "block_window"
    assert result.chunker_version == 1
    assert result.source_parse_fingerprint == parsed.parse_fingerprint
    assert len(result.chunk_set_fingerprint) == 64
    assert result.chunk_count == 1  # 3 短 block 全合并

    async with env["sessionmaker"]() as session:
        chunk_set = await ChunkSetRepository(session).get_by_parsed_source_id(
            parsed.parsed_source_id
        )
    assert chunk_set is not None and chunk_set.chunk_set_id == result.chunk_set_id
    chunks = await _chunks(env, result.chunk_set_id)
    assert len(chunks) == 1
    blocks = await _blocks_by_ordinal(env, parsed.parsed_source_id)
    chunk = chunks[0]
    assert chunk.ordinal == 1
    assert chunk.text == "\n".join(b.text for b in sorted(blocks.values(), key=lambda x: x.ordinal))
    assert chunk.text_sha256 == hashlib.sha256(chunk.text.encode("utf-8")).hexdigest()
    assert chunk.char_count == len(chunk.text)
    assert len(chunk.locator_refs) == 3
    # locator 类型全部 html_dom
    assert all(ref["locator"]["type"] == "html_dom" for ref in chunk.locator_refs)
    await _verify_chunk_trace(env, chunk, parsed.parsed_source_id, blocks)


async def test_multi_block_multi_chunk_traceback(env) -> None:
    """5×200 字 HTML → 3 chunks [401, 401, 200]，每个 chunk 都可回溯。"""
    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    result = await _chunk_service(env).chunk_parsed_source(parsed.parsed_source_id)

    assert result.replayed is False
    assert result.chunk_count == 3
    chunks = await _chunks(env, result.chunk_set_id)
    assert [c.char_count for c in chunks] == [401, 401, 200]
    assert [len(c.locator_refs) for c in chunks] == [2, 2, 1]
    blocks = await _blocks_by_ordinal(env, parsed.parsed_source_id)
    for chunk in chunks:
        await _verify_chunk_trace(env, chunk, parsed.parsed_source_id, blocks)


# ---------------------------------------------------------------- PDF


async def test_pdf_parsed_source_chunked_with_page_locator(env) -> None:
    source_id, _, _ = await _seed_pdf_source(env)
    parsed = await _parse(env, source_id)
    result = await _chunk_service(env).chunk_parsed_source(parsed.parsed_source_id)

    assert result.replayed is False
    assert result.chunk_count == 1  # 3 短行合并
    chunks = await _chunks(env, result.chunk_set_id)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert len(chunk.locator_refs) == 3
    for ref in chunk.locator_refs:
        locator = ref["locator"]
        assert locator["type"] == "pdf_page"
        assert locator["page_number"] == 1
        assert locator["page_width"] == 612.0
        assert locator["page_height"] == 792.0
    blocks = await _blocks_by_ordinal(env, parsed.parsed_source_id)
    await _verify_chunk_trace(env, chunk, parsed.parsed_source_id, blocks)


# ---------------------------------------------------------------- 不修改上游


async def test_source_record_and_parsed_source_not_modified(env) -> None:
    source_id, _, _ = await _seed_html_source(env)
    parsed_result = await _parse_service(env).parse_source(source_id)
    parsed_source_id = parsed_result.parsed_source_id

    async with env["sessionmaker"]() as session:
        before_parsed = await ParsedSourceRepository(session).get_by_id(parsed_source_id)
        before_source = await SourceRecordRepository(session).get_by_id(source_id)

    await _chunk_service(env).chunk_parsed_source(parsed_source_id)

    async with env["sessionmaker"]() as session:
        after_parsed = await ParsedSourceRepository(session).get_by_id(parsed_source_id)
        after_source = await SourceRecordRepository(session).get_by_id(source_id)
    assert after_parsed.block_count == before_parsed.block_count
    assert after_parsed.parse_fingerprint == before_parsed.parse_fingerprint
    assert after_parsed.raw_content_sha256 == before_parsed.raw_content_sha256
    assert after_source.title == before_source.title
    assert after_source.published_at == before_source.published_at
    assert after_source.artifact_id == before_source.artifact_id


# ---------------------------------------------------------------- replay


async def test_replay_reuses_same_chunk_set(env) -> None:
    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    service = _chunk_service(env)

    first = await service.chunk_parsed_source(parsed.parsed_source_id)
    assert first.replayed is False
    second = await service.chunk_parsed_source(parsed.parsed_source_id)
    assert second.replayed is True
    assert second.chunk_set_id == first.chunk_set_id
    assert second.chunk_set_fingerprint == first.chunk_set_fingerprint
    assert second.chunk_count == first.chunk_count

    sets, chunks = await _counts(env)
    assert sets == 1
    assert chunks == 3  # replay 不重复插 chunks


# ---------------------------------------------------------------- 版本敏感


async def test_chunker_version_change_creates_new_chunk_set_keeps_old(env, monkeypatch) -> None:
    """chunker version 升级 → 新 fingerprint → 新 ChunkSet；旧 v1 保留。"""
    import app.chunking.chunker as chunker_mod
    import app.chunking.contracts as contracts_mod

    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    service = _chunk_service(env)

    v1 = await service.chunk_parsed_source(parsed.parsed_source_id)
    assert v1.chunker_version == 1

    # 模拟 chunker v2（真实 chunker + 真实 fingerprint，仅版本号压到 2）。
    monkeypatch.setattr(contracts_mod, "CHUNKER_VERSION", 2)
    monkeypatch.setattr(chunker_mod, "CHUNKER_VERSION", 2)
    v2 = await service.chunk_parsed_source(parsed.parsed_source_id)
    monkeypatch.undo()

    assert v2.replayed is False
    assert v2.chunker_version == 2
    assert v2.chunk_set_id != v1.chunk_set_id
    assert v2.chunk_set_fingerprint != v1.chunk_set_fingerprint
    assert v2.parsed_source_id == v1.parsed_source_id

    sets, chunks = await _counts(env)
    assert sets == 2  # v1 + v2 ChunkSet 并存
    assert chunks == 6  # 各自一套 3 chunks

    # 恢复 v1 后再 chunk → replay v1（v1 指纹仍命中原 ChunkSet）。
    v1_again = await service.chunk_parsed_source(parsed.parsed_source_id)
    assert v1_again.replayed is True
    assert v1_again.chunk_set_id == v1.chunk_set_id


# ---------------------------------------------------------------- 并发


async def test_concurrent_chunking_single_chunk_set(env) -> None:
    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    service = _chunk_service(env)

    results = await asyncio.gather(
        service.chunk_parsed_source(parsed.parsed_source_id),
        service.chunk_parsed_source(parsed.parsed_source_id),
    )

    assert len({r.chunk_set_id for r in results}) == 1
    assert {r.replayed for r in results} == {False, True}
    assert results[0].chunk_set_fingerprint == results[1].chunk_set_fingerprint

    sets, chunks = await _counts(env)
    assert sets == 1
    assert chunks == 3


# ---------------------------------------------------------------- 完整性损坏


async def test_tampered_chunk_text_raises_integrity_error(env) -> None:
    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    service = _chunk_service(env)
    first = await service.chunk_parsed_source(parsed.parsed_source_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            update(DocumentChunkModel)
            .where(DocumentChunkModel.chunk_set_id == first.chunk_set_id)
            .where(DocumentChunkModel.ordinal == 1)
            .values(text="被篡改的内容")
        )
        await session.commit()

    with pytest.raises(ChunkSetIntegrityError) as exc:
        await service.chunk_parsed_source(parsed.parsed_source_id)
    assert exc.value.code == "chunk_set_integrity_error"

    # 不自动修复：篡改残留。
    sets, chunks = await _counts(env)
    assert sets == 1
    assert chunks == 3
    db_chunks = await _chunks(env, first.chunk_set_id)
    assert db_chunks[0].text == "被篡改的内容"


async def test_tampered_chunk_set_count_raises_integrity_error(env) -> None:
    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    service = _chunk_service(env)
    first = await service.chunk_parsed_source(parsed.parsed_source_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            update(ChunkSetModel)
            .where(ChunkSetModel.chunk_set_id == first.chunk_set_id)
            .values(chunk_count=99)
        )
        await session.commit()

    with pytest.raises(ChunkSetIntegrityError):
        await service.chunk_parsed_source(parsed.parsed_source_id)
    sets, _ = await _counts(env)
    assert sets == 1  # 未静默重建


async def test_tampered_locator_refs_raises_integrity_error(env) -> None:
    source_id, _, _ = await _seed_html_source(env, html=_MULTI_HTML)
    parsed = await _parse(env, source_id)
    service = _chunk_service(env)
    first = await service.chunk_parsed_source(parsed.parsed_source_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            update(DocumentChunkModel)
            .where(DocumentChunkModel.chunk_set_id == first.chunk_set_id)
            .where(DocumentChunkModel.ordinal == 1)
            .values(locator_refs=[{"block_ordinal": 99, "char_start": 0, "char_end": 5}])
        )
        await session.commit()

    with pytest.raises(ChunkSetIntegrityError):
        await service.chunk_parsed_source(parsed.parsed_source_id)
    sets, _ = await _counts(env)
    assert sets == 1


# ---------------------------------------------------------------- 错误路径


async def test_parsed_source_not_found(env) -> None:
    with pytest.raises(ParsedSourceNotFound) as exc:
        await _chunk_service(env).chunk_parsed_source(uuid4())
    assert exc.value.code == "parsed_source_not_found"
