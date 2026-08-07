"""E2E integration tests for SourceParsingService (stage 2E.1 / 2E.2, §8 persistence)。

需要真实 PostgreSQL（127.0.0.1:5433）。真实 LocalRawArtifactStore + 真实
SourceParsingService + 真实 Parser，零真实网络（conftest autouse guard 兜底）。
覆盖：
- first parse（HTML + PDF）：创建 ParsedSource + ParsedSourceBlock 快照
  （title/published_at/block_count/locator 正确），SourceRecord 元数据不被回写；
- replay：同 source + 同 RawArtifact + 同 parser version → 同 fingerprint →
  复用原快照（replayed=True），不重复插 Blocks；
- RawArtifact 永久不可变、SourceRecord 固定引用其 artifact：原始内容变化
  必须由新 RawArtifact + 新 SourceRecord 表达 → source_v1/artifact_v1 与
  source_v2/artifact_v2 内容不同 → 各自独立 ParsedSource，旧记录零 UPDATE；
- parser version 变化（同 source + 同 RawArtifact）→ 新 fingerprint → 新快照，
  旧快照保留；
- 完整性损坏（block text_sha256 被篡改 / snapshot block_count 不一致 /
  存储文件与 artifact 登记的 SHA 不一致）→ ParsedSourceIntegrityError，
  不自动修复；其中存储 SHA 不一致是存储层损坏/篡改检测，不是原文更新；
- 并发相同 parse → 只产生 1 个 ParsedSource + 一套 Blocks；
- blocks ordinal 连续 1..n 稳定；
- HTML vs PDF：parser_name 分别为 html_dom / pdf_layout，独立快照；
- 非 text/html / application/pdf artifact → UnsupportedParseMediaType；
  Source 不存在 → SourceRecordNotFound。
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from io import BytesIO
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import func, select, text, update

from app.core.config import get_settings
from app.core.errors import SourceRecordNotFound
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.parsing.contracts import (
    HTML_PARSER_NAME,
    HTML_PARSER_VERSION,
    PDF_PARSER_NAME,
    PDF_PARSER_VERSION,
)
from app.parsing.errors import (
    ParsedSourceIntegrityError,
    PdfEncryptedError,
    UnsupportedParseMediaType,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.parsed_source_block_repository import (
    ParsedSourceBlockRepository,
)
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.pdf_fixtures import encrypted_pdf, single_page_pdf

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"
_XINHUA_URL_2 = "https://www.xinhuanet.com/2026/0807/0002.htm"
# og:title 覆盖 <title>；article:published_time 携带 +08:00 偏移（机器可读）。
_HTML = (
    "<html><head>"
    '<meta property="og:title" content="确定性解析标题">'
    '<meta property="article:published_time" content="2026-08-07T09:30:00+08:00">'
    "<title>文档标题</title>"
    "</head><body><article>"
    "<h1>确定性解析标题</h1>"
    "<p>第一段正文。</p>"
    "<ul><li>列表项一</li><li>列表项二</li></ul>"
    "</article></body></html>"
).encode()

_JSON_ARTIFACT = b'{"url": "https://www.xinhuanet.com/2026/0807/0002.htm", "rank": 1}'
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)  # SourceRecord 侧固定值


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
    # 确保 xinhuanet 等默认 Provider 存在（upsert，不破坏其他测试）。
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


async def _seed_html_source(
    env: dict,
    *,
    html: bytes = _HTML,
    source_url: str = _XINHUA_URL,
    media_type: str = "text/html",
) -> tuple:
    """真实 LocalRawArtifactStore 落盘 + 真实 Repository 登记 SourceRecord。

    默认 text/html；media_type="application/json" 时用 JSON artifact
    （用于非 HTML 拒绝路径）。返回 (source_id, artifact_id, storage_key)。
    """
    if media_type == "application/json":
        stored = env["raw_store"].put_json_bytes(_JSON_ARTIFACT)
        document_type = "news_article"
        provider_key = "xinhuanet"
    else:
        stored = env["raw_store"].put_html_bytes(html)
        document_type = "news_article"
        provider_key = "xinhuanet"
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:  # 并发/残留冲突：复用既有行
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=env["company_id"],
            provider_key=provider_key,
            artifact_id=artifact.artifact_id,
            document_type=document_type,
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
    env: dict,
    *,
    pdf: bytes = single_page_pdf(title="季度报告"),
    source_url: str = _XINHUA_URL,
) -> tuple:
    """真实 LocalRawArtifactStore 落盘 PDF（put_pdf_stream）+ 真实 SourceRecord。

    media_type=application/pdf；返回 (source_id, artifact_id, storage_key)。
    """
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
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=_PUBLISHED_AT,
            source_url=source_url,
            acquisition_method="user_upload",  # CHECK 仅允许三种来源
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        return record.source_id, artifact.artifact_id, stored.storage_key


def _service(env: dict) -> SourceParsingService:
    return SourceParsingService(env["sessionmaker"], env["raw_store"])


async def _get_parsed_source(env: dict, source_id):
    async with env["sessionmaker"]() as session:
        return await ParsedSourceRepository(session).get_by_source_id(source_id)


async def _get_blocks(env: dict, parsed_source_id):
    async with env["sessionmaker"]() as session:
        return await ParsedSourceBlockRepository(session).list_for_parsed_source(parsed_source_id)


async def _counts(env: dict) -> tuple:
    async with env["sessionmaker"]() as session:
        parsed = (
            await session.execute(select(func.count(ParsedSourceModel.parsed_source_id)))
        ).scalar_one()
        blocks = (
            await session.execute(select(func.count(ParsedSourceBlockModel.block_id)))
        ).scalar_one()
    return parsed, blocks


async def _get_source(env: dict, source_id):
    async with env["sessionmaker"]() as session:
        return await SourceRecordRepository(session).get_by_id(source_id)


# ---------------------------------------------------------------- first parse


async def test_first_parse_creates_snapshot_and_blocks(env) -> None:
    source_id, artifact_id, storage_key = await _seed_html_source(env)
    result = await _service(env).parse_source(source_id)

    assert result.replayed is False
    assert result.source_id == source_id
    assert result.artifact_id == artifact_id
    assert result.parser_name == HTML_PARSER_NAME
    assert result.parser_version == HTML_PARSER_VERSION
    assert result.raw_content_sha256 == hashlib.sha256(_HTML).hexdigest()
    assert result.extracted_title == "确定性解析标题"  # og:title 优先
    # PG timestamptz 统一按 UTC 规范化存储，+08:00 → 01:30 UTC。
    assert result.extracted_published_at is not None
    assert result.extracted_published_at.utcoffset() is not None
    assert result.extracted_published_at.astimezone(UTC).isoformat() == (
        "2026-08-07T01:30:00+00:00"
    )
    assert result.block_count == 4

    snapshot = await _get_parsed_source(env, source_id)
    assert snapshot is not None
    assert snapshot.parsed_source_id == result.parsed_source_id
    assert snapshot.raw_content_sha256 == result.raw_content_sha256
    assert len(snapshot.parse_fingerprint) == 64

    blocks = await _get_blocks(env, snapshot.parsed_source_id)
    assert [(b.ordinal, b.block_type, b.text) for b in blocks] == [
        (1, "heading", "确定性解析标题"),
        (2, "paragraph", "第一段正文。"),
        (3, "list_item", "列表项一"),
        (4, "list_item", "列表项二"),
    ]
    # 文本哈希与原文 SHA-256 一致（确定性）
    for block in blocks:
        assert block.text_sha256 == hashlib.sha256(block.text.encode("utf-8")).hexdigest()
    # locator 绝对 xpath（同 tag 兄弟仅一个时不加下标，多时才加 [N]）
    assert [b.locator["xpath"] for b in blocks] == [
        "/html/body/article/h1",
        "/html/body/article/p",
        "/html/body/article/ul/li[1]",
        "/html/body/article/ul/li[2]",
    ]
    assert all(b.locator["type"] == "html_dom" for b in blocks)
    assert all(b.locator["ordinal"] == b.ordinal for b in blocks)


async def test_source_record_metadata_not_written_back(env) -> None:
    """解析出的 title/published_at 只进 ParsedSource，SourceRecord 保持原始值。"""
    source_id, _, _ = await _seed_html_source(env)
    await _service(env).parse_source(source_id)

    source = await _get_source(env, source_id)
    assert source is not None
    assert source.title == _SOURCE_TITLE  # 不被 og:title 覆盖
    assert source.published_at == _PUBLISHED_AT  # 不被 article:published_time 覆盖


# ---------------------------------------------------------------- replay


async def test_replay_reuses_same_snapshot(env) -> None:
    source_id, _, _ = await _seed_html_source(env)
    service = _service(env)

    first = await service.parse_source(source_id)
    assert first.replayed is False
    second = await service.parse_source(source_id)
    assert second.replayed is True
    assert second.parsed_source_id == first.parsed_source_id
    assert second.parse_fingerprint == first.parse_fingerprint
    assert second.block_count == first.block_count

    parsed, blocks = await _counts(env)
    assert parsed == 1
    assert blocks == 4  # replay 不重复插 Blocks


# ---------------------------------------------------------------- fingerprint / 版本


async def test_parser_version_change_creates_new_snapshot_keeps_old(env, monkeypatch) -> None:
    """parser version 升级 → 新 fingerprint → 新快照；旧 v1 快照保留（可追溯）。

    当前真实 html_dom VERSION=2（Gate 0）。先 monkeypatch 成 1 建 v1 快照，
    再恢复真实 v2：v1 快照不修改不删除，v2 是新快照（replay 不复用 v1）。
    """
    import app.parsing.contracts as contracts_mod
    import app.parsing.html_parser as parser_mod

    source_id, _, _ = await _seed_html_source(env)
    service = _service(env)

    # 模拟 v1 parser（真实 parser + 真实 fingerprint，仅把版本号压回 1）。
    monkeypatch.setattr(parser_mod, "HTML_PARSER_VERSION", 1)
    monkeypatch.setattr(contracts_mod, "HTML_PARSER_VERSION", 1)
    v1 = await service.parse_source(source_id)
    assert v1.parser_version == 1
    monkeypatch.undo()  # 恢复真实 VERSION=2

    v2 = await service.parse_source(source_id)
    assert v2.replayed is False
    assert v2.parser_version == HTML_PARSER_VERSION  # 真实 v2
    assert v2.parse_fingerprint != v1.parse_fingerprint
    assert v2.parsed_source_id != v1.parsed_source_id
    assert v2.block_count == v1.block_count == 4

    parsed, blocks = await _counts(env)
    assert parsed == 2  # 新旧（v1 + v2）快照并存
    assert blocks == 8  # 各自一套 blocks


async def test_distinct_raw_artifacts_produce_distinct_snapshots(env) -> None:
    """原始内容变化必须由新 RawArtifact + 新 SourceRecord 表达（RawArtifact 不可变）。

    source_v1/artifact_v1 与 source_v2/artifact_v2 内容不同 → 各自独立的
    ParsedSource；旧 RawArtifact / SourceRecord 全程零 UPDATE。
    """
    html_v1 = _HTML
    html_v2 = _HTML.replace("第一段正文。".encode(), "第一段正文（修订）。".encode())
    assert html_v1 != html_v2
    source_v1, artifact_v1, storage_key_v1 = await _seed_html_source(
        env, html=html_v1, source_url=_XINHUA_URL
    )
    source_v2, artifact_v2, storage_key_v2 = await _seed_html_source(
        env, html=html_v2, source_url=_XINHUA_URL_2
    )
    service = _service(env)

    r1 = await service.parse_source(source_v1)
    r2 = await service.parse_source(source_v2)

    assert r1.replayed is False and r2.replayed is False
    assert r1.parsed_source_id != r2.parsed_source_id
    assert r1.parse_fingerprint != r2.parse_fingerprint
    assert r1.raw_content_sha256 == hashlib.sha256(html_v1).hexdigest()
    assert r2.raw_content_sha256 == hashlib.sha256(html_v2).hexdigest()
    assert r1.artifact_id == artifact_v1
    assert r2.artifact_id == artifact_v2
    assert r1.block_count == r2.block_count == 4

    parsed, blocks = await _counts(env)
    assert parsed == 2
    assert blocks == 8

    # RawArtifact / SourceRecord 不可变：两个 source 各自引用各自 artifact，
    # 旧记录内容、SHA、storage_key、引用全部保持原值（零 UPDATE）。
    async with env["sessionmaker"]() as session:
        a1 = await RawArtifactRepository(session).get_by_id(artifact_v1)
        a2 = await RawArtifactRepository(session).get_by_id(artifact_v2)
        assert a1 is not None and a2 is not None
        assert a1.content_sha256 == hashlib.sha256(html_v1).hexdigest()
        assert a1.byte_size == len(html_v1)
        assert a1.storage_key == storage_key_v1
        assert a2.content_sha256 == hashlib.sha256(html_v2).hexdigest()
        assert a2.byte_size == len(html_v2)
        assert a2.storage_key == storage_key_v2
        s1 = await SourceRecordRepository(session).get_by_id(source_v1)
        s2 = await SourceRecordRepository(session).get_by_id(source_v2)
        assert s1 is not None and s2 is not None
        assert s1.artifact_id == artifact_v1
        assert s2.artifact_id == artifact_v2


# ---------------------------------------------------------------- 完整性损坏


async def test_tampered_block_text_sha256_raises_integrity_error(env) -> None:
    """replay 校验发现 block 哈希被篡改 → ParsedSourceIntegrityError，不自动修复。"""
    source_id, _, _ = await _seed_html_source(env)
    service = _service(env)
    first = await service.parse_source(source_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            update(ParsedSourceBlockModel)
            .where(ParsedSourceBlockModel.parsed_source_id == first.parsed_source_id)
            .where(ParsedSourceBlockModel.ordinal == 2)
            .values(text_sha256="0" * 64)
        )
        await session.commit()

    with pytest.raises(ParsedSourceIntegrityError) as exc:
        await service.parse_source(source_id)
    assert exc.value.code == "parsed_source_integrity_error"

    # 不自动修复：篡改残留，快照未被重建
    parsed, blocks = await _counts(env)
    assert parsed == 1
    assert blocks == 4
    tampered = await _get_blocks(env, first.parsed_source_id)
    assert tampered[1].text_sha256 == "0" * 64


async def test_tampered_snapshot_block_count_raises_integrity_error(env) -> None:
    """snapshot.block_count 与 blocks 实际数不一致 → integrity error（不修）。"""
    source_id, _, _ = await _seed_html_source(env)
    service = _service(env)
    first = await service.parse_source(source_id)

    async with env["sessionmaker"]() as session:
        await session.execute(
            update(ParsedSourceModel)
            .where(ParsedSourceModel.parsed_source_id == first.parsed_source_id)
            .values(block_count=99)
        )
        await session.commit()

    with pytest.raises(ParsedSourceIntegrityError):
        await service.parse_source(source_id)
    parsed, _ = await _counts(env)
    assert parsed == 1


async def test_storage_content_sha_mismatch_raises_integrity_error(env) -> None:
    """存储文件与 artifact 登记的 SHA 不一致 → integrity error（内容寻址被篡改）。

    这是存储层损坏/篡改检测（RawArtifact 不可变 ⇒ 文件与登记不一致即损坏），
    不是原文更新语义；正常的原始内容变化必须走新 RawArtifact + 新 SourceRecord。
    """
    source_id, _, storage_key = await _seed_html_source(env)
    # 故障注入：改存储文件但不改 artifact.content_sha256 登记值。
    path = env["raw_root"] / storage_key
    path.write_bytes("<html><body><p>被替换的内容</p></body></html>".encode())

    with pytest.raises(ParsedSourceIntegrityError) as exc:
        await _service(env).parse_source(source_id)
    assert exc.value.code == "parsed_source_integrity_error"


# ---------------------------------------------------------------- 并发


async def test_concurrent_parse_single_snapshot(env) -> None:
    source_id, _, _ = await _seed_html_source(env)
    service = _service(env)

    results = await asyncio.gather(service.parse_source(source_id), service.parse_source(source_id))

    assert len({r.parsed_source_id for r in results}) == 1
    assert {r.replayed for r in results} == {False, True}  # 一个赢家 + 一个 replay
    assert results[0].parse_fingerprint == results[1].parse_fingerprint

    parsed, blocks = await _counts(env)
    assert parsed == 1
    assert blocks == 4


# ---------------------------------------------------------------- blocks 稳定


async def test_blocks_ordinal_contiguous_and_stable(env) -> None:
    source_id, _, _ = await _seed_html_source(env)
    result = await _service(env).parse_source(source_id)

    blocks = await _get_blocks(env, result.parsed_source_id)
    assert [b.ordinal for b in blocks] == list(range(1, result.block_count + 1))
    assert [b.ordinal for b in blocks] == [1, 2, 3, 4]
    # 相邻 block (type, text) 不允许完全相同（parser 已去重）
    for index in range(len(blocks) - 1):
        left, right = blocks[index], blocks[index + 1]
        assert (left.block_type, left.text) != (right.block_type, right.text)


# ---------------------------------------------------------------- 错误路径


async def test_non_html_artifact_rejected(env) -> None:
    source_id, _, _ = await _seed_html_source(
        env, source_url=_XINHUA_URL_2, media_type="application/json"
    )
    with pytest.raises(UnsupportedParseMediaType) as exc:
        await _service(env).parse_source(source_id)
    assert exc.value.code == "unsupported_parse_media_type"


async def test_source_not_found(env) -> None:
    with pytest.raises(SourceRecordNotFound) as exc:
        await _service(env).parse_source(uuid4())
    assert exc.value.code == "source_record_not_found"


# ---------------------------------------------------------------- PDF（2E.2）


async def test_pdf_first_parse_creates_snapshot_and_blocks(env) -> None:
    pdf = single_page_pdf(title="季度报告")
    source_id, artifact_id, _ = await _seed_pdf_source(env, pdf=pdf)
    result = await _service(env).parse_source(source_id)

    assert result.replayed is False
    assert result.parser_name == PDF_PARSER_NAME
    assert result.parser_version == PDF_PARSER_VERSION
    assert result.raw_content_sha256 == hashlib.sha256(pdf).hexdigest()
    assert result.extracted_title == "季度报告"  # metadata Title
    assert result.extracted_published_at is None  # 绝不使用 CreationDate/ModDate
    assert result.block_count == 3

    snapshot = await _get_parsed_source(env, source_id)
    assert snapshot is not None
    assert snapshot.parser_name == PDF_PARSER_NAME
    assert snapshot.raw_content_sha256 == result.raw_content_sha256

    blocks = await _get_blocks(env, snapshot.parsed_source_id)
    assert [(b.ordinal, b.block_type, b.text) for b in blocks] == [
        (1, "paragraph", "Hello world"),
        (2, "paragraph", "Second line"),
        (3, "paragraph", "Third line"),
    ]
    for block in blocks:
        assert block.text_sha256 == hashlib.sha256(block.text.encode("utf-8")).hexdigest()
        locator = block.locator
        assert locator["type"] == "pdf_page"
        assert locator["page_number"] == 1
        assert locator["line_index"] == block.ordinal
        assert locator["page_width"] == 612.0
        assert locator["page_height"] == 792.0
        x0, top, x1, bottom = locator["bbox"]
        assert 0.0 <= x0 <= x1 <= 612.0
        assert 0.0 <= top <= bottom <= 792.0


async def test_pdf_replay_reuses_same_snapshot(env) -> None:
    source_id, _, _ = await _seed_pdf_source(env)
    service = _service(env)

    first = await service.parse_source(source_id)
    assert first.replayed is False
    second = await service.parse_source(source_id)
    assert second.replayed is True
    assert second.parsed_source_id == first.parsed_source_id
    assert second.parse_fingerprint == first.parse_fingerprint

    parsed, blocks = await _counts(env)
    assert parsed == 1
    assert blocks == 3  # replay 不重复插 Blocks


async def test_pdf_source_record_metadata_not_written_back(env) -> None:
    source_id, _, _ = await _seed_pdf_source(env, pdf=single_page_pdf(title="PDF标题"))
    await _service(env).parse_source(source_id)

    source = await _get_source(env, source_id)
    assert source is not None
    assert source.title == _SOURCE_TITLE  # 不被 PDF metadata Title 覆盖
    assert source.published_at == _PUBLISHED_AT


async def test_html_and_pdf_produce_distinct_parser_snapshots(env) -> None:
    """同公司两个 Source：HTML → html_dom v2，PDF → pdf_layout v1，独立快照。"""
    html_source, _, _ = await _seed_html_source(env, source_url=_XINHUA_URL)
    pdf_source, _, _ = await _seed_pdf_source(env, source_url=_XINHUA_URL_2)
    service = _service(env)

    html_result = await service.parse_source(html_source)
    pdf_result = await service.parse_source(pdf_source)

    assert html_result.parser_name == HTML_PARSER_NAME
    assert html_result.parser_version == HTML_PARSER_VERSION
    assert pdf_result.parser_name == PDF_PARSER_NAME
    assert pdf_result.parser_version == PDF_PARSER_VERSION
    assert html_result.parsed_source_id != pdf_result.parsed_source_id
    assert html_result.parse_fingerprint != pdf_result.parse_fingerprint

    parsed, blocks = await _counts(env)
    assert parsed == 2
    assert blocks == 4 + 3  # HTML 4 blocks + PDF 3 blocks


async def test_pdf_encrypted_raises_encrypted_error(env) -> None:
    source_id, _, _ = await _seed_pdf_source(env, pdf=encrypted_pdf())
    with pytest.raises(PdfEncryptedError) as exc:
        await _service(env).parse_source(source_id)
    assert exc.value.code == "pdf_encrypted_error"


async def test_pdf_malformed_raises_parse_error(env) -> None:
    # magic 有效但结构损坏：put_pdf_stream 只校验 %PDF- 头，解析时失败。
    malformed = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
    source_id, _, _ = await _seed_pdf_source(env, pdf=malformed)
    from app.parsing.errors import PdfParseError

    with pytest.raises(PdfParseError):
        await _service(env).parse_source(source_id)
