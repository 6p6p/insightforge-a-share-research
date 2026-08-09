"""EvidenceCardService integration tests (stage 3C.1, spec 13/14).

需要真实 PostgreSQL（127.0.0.1:5433）。HTML 用真实 SourceParsingService +
ChunkingService（零 Chroma / 零 LLM / 零 embedding）；PDF 用手工 seed
ParsedSource + pdf_page locator blocks + ChunkingService。

覆盖：
- first create / replay（同 fingerprint 复用同一卡，replayed=True）/ 并发→1；
- 语义变化（statement / quote range / extractor version）→ 新卡，旧卡保留；
- provenance snapshots（company/source/parsed/chunk_set/chunk IDs、provider、
  authority tier、published、reporting period）；
- extractor_confidence=high 不会自动提升 critical_claim_eligible_snapshot
  （直接复制 SourceRecord）；
- 损坏 replay → EvidenceCardIntegrityError，**不自动 repair**；无 update API；
- E2E HTML：SourceRecord→ParsedSource→Chunk→EvidenceCard→DOM locator→
  SourceRecord/RawArtifact 完整回溯；quote 精确切片；
- E2E PDF：page/bbox locator 跨页保留，回溯到 ParsedSourceBlock；
- provenance 链断裂 → EvidenceProvenanceIntegrityError；
- 边界：不创建 Claim/Report/ReviewIssue；不碰 LLM/LangGraph/CrewAI/BGE/
  Chroma（0 manifest）；EvidenceCard 不是 RetrievalHit 的自动升级
  （Service 只显式接受 EvidenceCardDraft）。
"""

import asyncio
import hashlib
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.evidence.errors import (
    EvidenceCardIntegrityError,
    EvidenceProvenanceIntegrityError,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XINHUA_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_PERIOD_END = date(2024, 12, 31)

# 5×200 字 HTML → 3 chunks [401, 401, 200]，文本互不相同。
_MULTI_HTML = (
    "<html><head><title>多段文档</title></head><body><article>"
    + "".join(f"<p>{'甲乙丙丁戊'[i % 5] * 200}</p>" for i in range(5))
    + "</article></body></html>"
).encode()

# PDF 手工 seed：两个 block（13 / 10 字），合并后 chunk.text = b1 + "\n" + b2（24 字）。
_PDF_B1 = "贵州茅台2024年营业收入"
_PDF_B2 = "归属净利润862亿元"

_QUESTION = "2024年贵州茅台净利润增长情况？"
_STATEMENT = "2024年贵州茅台归属净利润同比增长15%。"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


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
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM chunk_vector_indexes"))
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
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
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
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_html_source(
    env: dict,
    *,
    provider_key="xinhuanet",
    document_type="news_article",
    authority_tier=3,
    critical_claim_eligible=False,
    published_at=_PUBLISHED_AT,
    reporting_period_end=None,
    source_url=_XINHUA_URL,
) -> tuple:
    """真实 HTML：SourceRecord → ParsedSource(html_dom v2) → ChunkSet → Chunks。"""
    stored = env["raw_store"].put_html_bytes(_MULTI_HTML)
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
            provider_key=provider_key,
            artifact_id=artifact.artifact_id,
            document_type=document_type,
            title=_SOURCE_TITLE,
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            source_url=source_url,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=authority_tier,
            critical_claim_eligible_snapshot=critical_claim_eligible,
            provider_capabilities_snapshot=[document_type],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsed_service = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsed_service.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "HTML seed must produce chunks"
    return source_id, parsed.parsed_source_id, result.chunk_set_id, chunks


async def _seed_pdf_source(
    env: dict,
    *,
    provider_key="sse",
    authority_tier=1,
    critical_claim_eligible=True,
    published_at=_PUBLISHED_AT,
    reporting_period_end=_PERIOD_END,
) -> tuple:
    """手工 PDF：SourceRecord → ParsedSource(pdf_layout v2) + 2 blocks → ChunkSet。"""
    dummy_bytes = f"<html><body>pdf-seed {uuid4().hex}</body></html>".encode()
    stored = env["raw_store"].put_html_bytes(dummy_bytes)
    artifact_id = uuid4()
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
        artifact_id = artifact.artifact_id
        record = SourceRecordModel(
            company_id=env["company_id"],
            provider_key=provider_key,
            artifact_id=artifact.artifact_id,
            document_type="company_announcement",
            title="PDF标题",
            published_at=published_at,
            reporting_period_end=reporting_period_end,
            source_url="https://www.sse.com.cn/2026/0809/0001.pdf",
            acquisition_method="user_upload",
            status="available",
            authority_tier_snapshot=authority_tier,
            critical_claim_eligible_snapshot=critical_claim_eligible,
            provider_capabilities_snapshot=["company_announcement"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id

    parsed_id = uuid4()
    parse_fingerprint = _sha(f"pdf-{uuid4().hex}")
    blocks = [
        (
            1,
            _PDF_B1,
            {
                "type": "pdf_page",
                "page_number": 1,
                "line_index": 3,
                "bbox": [50.0, 100.0, 200.0, 120.0],
                "page_width": 595.0,
                "page_height": 842.0,
            },
        ),
        (
            2,
            _PDF_B2,
            {
                "type": "pdf_page",
                "page_number": 2,
                "line_index": 1,
                "bbox": [30.0, 80.0, 300.0, 100.0],
                "page_width": 595.0,
                "page_height": 842.0,
            },
        ),
    ]
    async with env["sessionmaker"]() as session:
        session.add(
            ParsedSourceModel(
                parsed_source_id=parsed_id,
                source_id=source_id,
                artifact_id=artifact_id,
                parser_name="pdf_layout",
                parser_version=2,
                raw_content_sha256=stored.content_sha256,
                parse_fingerprint=parse_fingerprint,
                extracted_title="PDF标题",
                extracted_published_at=None,
                block_count=2,
                parsed_at=datetime.now(UTC),
            )
        )
        await session.flush()
        for ordinal, block_text, locator in blocks:
            session.add(
                ParsedSourceBlockModel(
                    block_id=uuid4(),
                    parsed_source_id=parsed_id,
                    ordinal=ordinal,
                    block_type="paragraph",
                    text=block_text,
                    text_sha256=_sha(block_text),
                    locator=locator,
                )
            )
        await session.commit()

    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed_id)
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "PDF seed must produce chunks"
    return source_id, parsed_id, result.chunk_set_id, chunks


def _draft(chunk, *, quote_start=0, quote_end=20, **overrides) -> EvidenceCardDraft:
    values = dict(
        research_question=_QUESTION,
        evidence_statement=_STATEMENT,
        evidence_type=EvidenceType.METRIC,
        chunk_id=chunk.chunk_id,
        quote_start=quote_start,
        quote_end=quote_end,
        extractor_name="test-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    values.update(overrides)
    return EvidenceCardDraft(**values)


async def _create_card(env, chunk, *, quote_start=0, quote_end=20, **overrides):
    draft = _draft(chunk, quote_start=quote_start, quote_end=quote_end, **overrides)
    return await EvidenceCardService(env["sessionmaker"]).create_card(draft)


async def _card_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM evidence_cards"))).scalar_one()
        )


# ---------------------------------------------------------------- persistence


async def test_first_create_persists_card(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    result = await _create_card(env, chunk, quote_start=0, quote_end=20)
    assert result.replayed is False
    assert len(result.evidence_fingerprint) == 64

    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
        assert card is not None
        assert card.chunk_id == chunk.chunk_id
        assert card.quote_text == chunk.text[0:20]
        assert card.quote_sha256 == _sha(chunk.text[0:20])
        assert card.evidence_fingerprint == result.evidence_fingerprint
        # 新 document 卡使用 schema v2（origin 模型泛化后 fingerprint 含 origin_type）。
        assert card.evidence_schema_version == 2
        assert card.origin_type == "document_chunk"


async def test_replay_returns_same_card(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    first = await _create_card(env, chunk)
    second = await _create_card(env, chunk)
    assert first.replayed is False
    assert second.replayed is True
    assert first.evidence_card_id == second.evidence_card_id
    assert first.evidence_fingerprint == second.evidence_fingerprint
    assert await _card_count(env["sessionmaker"]) == 1


async def test_concurrent_create_yields_single_card(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    draft = _draft(chunk)
    service = EvidenceCardService(env["sessionmaker"])
    results = await asyncio.gather(*(service.create_card(draft) for _ in range(5)))
    ids = {r.evidence_card_id for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r.replayed) == 4
    assert sum(1 for r in results if not r.replayed) == 1
    assert await _card_count(env["sessionmaker"]) == 1


async def test_statement_change_creates_new_card(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    a = await _create_card(env, chunk, evidence_statement="表述A")
    b = await _create_card(env, chunk, evidence_statement="表述B")
    assert a.evidence_card_id != b.evidence_card_id
    assert b.replayed is False
    assert await _card_count(env["sessionmaker"]) == 2  # 旧卡保留


async def test_quote_range_change_creates_new_card(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    a = await _create_card(env, chunk, quote_start=0, quote_end=20)
    b = await _create_card(env, chunk, quote_start=0, quote_end=40)
    assert a.evidence_card_id != b.evidence_card_id
    assert await _card_count(env["sessionmaker"]) == 2


async def test_extractor_version_change_creates_new_card(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    a = await _create_card(env, chunk, extractor_version=1)
    b = await _create_card(env, chunk, extractor_version=2)
    assert a.evidence_card_id != b.evidence_card_id
    assert await _card_count(env["sessionmaker"]) == 2


# ---------------------------------------------------------------- provenance snapshots


async def test_create_card_copies_provenance_snapshots(env) -> None:
    src, parsed_id, cs_id, chunks = await _seed_html_source(
        env,
        authority_tier=1,
        critical_claim_eligible=True,
        published_at=datetime(2026, 8, 1, tzinfo=UTC),
        reporting_period_end=_PERIOD_END,
    )
    chunk = chunks[0]
    result = await _create_card(env, chunk)

    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
        assert card is not None
        assert card.company_id == env["company_id"]
        assert card.source_id == src
        assert card.parsed_source_id == parsed_id
        assert card.chunk_set_id == cs_id
        assert card.chunk_id == chunk.chunk_id
        assert card.provider_key == "xinhuanet"
        assert card.authority_tier_snapshot == 1
        assert card.critical_claim_eligible_snapshot is True
        assert card.source_published_at == datetime(2026, 8, 1, tzinfo=UTC)
        assert card.reporting_period_end == _PERIOD_END


async def test_high_extractor_confidence_does_not_promote_critical_eligibility(env) -> None:
    # SourceRecord critical_claim_eligible=False：即使 extractor_confidence=high
    # 也不得自动提升（快照直接复制 SourceRecord）。
    _, _, _, chunks = await _seed_html_source(env, critical_claim_eligible=False)
    chunk = chunks[0]
    await _create_card(env, chunk, extractor_confidence=EvidenceConfidence.HIGH)

    async with env["sessionmaker"]() as session:
        card = (await session.execute(select(EvidenceCardModel))).scalar_one()
        assert card.critical_claim_eligible_snapshot is False


# ---------------------------------------------------------------- corruption replay


async def test_corrupted_replay_raises_integrity_error_and_no_repair(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    await _create_card(env, chunk)

    # 篡改已落库卡片的 evidence_statement（fingerprint 列不变）。
    async with env["sessionmaker"]() as session:
        await session.execute(text("UPDATE evidence_cards SET evidence_statement = '篡改'"))
        await session.commit()

    with pytest.raises(EvidenceCardIntegrityError):
        await _create_card(env, chunk)

    # 不自动 repair：篡改值仍在。
    async with env["sessionmaker"]() as session:
        value = (
            await session.execute(text("SELECT evidence_statement FROM evidence_cards"))
        ).scalar_one()
        assert value == "篡改"


async def test_corrupted_quote_replay_raises_integrity_error(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    await _create_card(env, chunk)

    async with env["sessionmaker"]() as session:
        await session.execute(text("UPDATE evidence_cards SET quote_text = '被篡改的引用'"))
        await session.commit()

    with pytest.raises(EvidenceCardIntegrityError):
        await _create_card(env, chunk)


async def test_repository_has_no_update_api(env) -> None:
    assert not hasattr(EvidenceCardRepository, "update")
    assert not hasattr(EvidenceCardRepository, "update_by_id")


# ---------------------------------------------------------------- E2E HTML


async def test_e2e_html_card_traceable_to_source_and_dom_locator(env) -> None:
    src, parsed_id, cs_id, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    result = await _create_card(env, chunk, quote_start=0, quote_end=20)

    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
        assert card is not None
        # quote 精确切片 + sha256
        assert card.quote_text == chunk.text[0:20]
        assert card.quote_sha256 == _sha(chunk.text[0:20])
        assert card.research_question == _QUESTION
        assert card.research_question_sha256 == _sha(_QUESTION)
        # quote 级 DOM locator
        assert len(card.locator_refs) == 1
        ref = card.locator_refs[0]
        assert ref["block_ordinal"] == 1
        assert ref["char_start"] == 0
        assert ref["char_end"] == 20
        assert ref["locator"]["type"] == "html_dom"
        assert ref["locator"]["xpath"].startswith("/html/body/article/p[")
        # 完整回溯：card → chunk → chunk_set → parsed → source → company/artifact
        trace = await session.execute(
            text(
                "SELECT ec.company_id AS company, ec.source_id AS source, "
                "       ec.parsed_source_id AS parsed, ec.chunk_set_id AS cs, "
                "       ec.chunk_id AS chunk, "
                "       s.company_id AS src_company, s.artifact_id AS src_artifact, "
                "       ps.source_id AS ps_source, cs.parsed_source_id AS cs_parsed, "
                "       dc.chunk_set_id AS dc_set "
                "FROM evidence_cards ec "
                "JOIN document_chunks dc ON dc.chunk_id = ec.chunk_id "
                "JOIN chunk_sets cs ON cs.chunk_set_id = ec.chunk_set_id "
                "JOIN parsed_sources ps ON ps.parsed_source_id = ec.parsed_source_id "
                "JOIN source_records s ON s.source_id = ec.source_id "
                "JOIN raw_artifacts ra ON ra.artifact_id = s.artifact_id "
                "WHERE ec.evidence_card_id = :cid"
            ).bindparams(cid=result.evidence_card_id)
        )
        row = trace.mappings().first()
        assert row is not None
        assert row["company"] == env["company_id"]
        assert row["src_company"] == env["company_id"]
        assert row["source"] == src
        assert row["ps_source"] == src
        assert row["parsed"] == parsed_id
        assert row["cs_parsed"] == parsed_id
        assert row["cs"] == cs_id
        assert row["dc_set"] == cs_id
        assert row["chunk"] == chunk.chunk_id
        assert row["src_artifact"] is not None


async def test_e2e_html_quote_crossing_newline_keeps_two_dom_locators(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]  # 401 chars = 200 + "\n" + 200
    assert len(chunk.text) == 401
    start, end = 195, 210
    result = await _create_card(env, chunk, quote_start=start, quote_end=end)

    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
        assert card is not None
        assert card.quote_text == chunk.text[start:end]
        assert len(card.locator_refs) == 2
        assert card.locator_refs[0]["locator"]["type"] == "html_dom"
        assert card.locator_refs[1]["locator"]["type"] == "html_dom"
        assert card.locator_refs[0]["char_end"] == 200
        assert card.locator_refs[1]["char_start"] == 0


# ---------------------------------------------------------------- E2E PDF


async def test_e2e_pdf_card_traceable_to_page_bbox_locator(env) -> None:
    src, parsed_id, cs_id, chunks = await _seed_pdf_source(env)
    chunk = chunks[0]
    assert chunk.text == _PDF_B1 + "\n" + _PDF_B2
    assert len(chunk.text) == len(_PDF_B1) + 1 + len(_PDF_B2)
    start = len(_PDF_B1) - 2
    end = len(_PDF_B1) + 1 + 3  # 跨 "\n"：block1 末尾 2 字 + block2 前 3 字
    result = await _create_card(env, chunk, quote_start=start, quote_end=end)

    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
        assert card is not None
        assert card.quote_text == chunk.text[start:end]
        assert len(card.locator_refs) == 2
        refs = card.locator_refs
        # page/bbox locator 原样保留 + 缩窄到原 block 字符范围
        assert refs[0]["block_ordinal"] == 1
        assert refs[0]["char_start"] == start
        assert refs[0]["char_end"] == len(_PDF_B1)
        assert refs[0]["locator"]["type"] == "pdf_page"
        assert refs[0]["locator"]["page_number"] == 1
        assert refs[0]["locator"]["bbox"] == [50.0, 100.0, 200.0, 120.0]
        assert refs[1]["block_ordinal"] == 2
        assert refs[1]["char_start"] == 0
        assert refs[1]["char_end"] == 3
        assert refs[1]["locator"]["page_number"] == 2
        assert refs[1]["locator"]["bbox"] == [30.0, 80.0, 300.0, 100.0]
        # 回溯到 ParsedSourceBlock（parsed_source_id + block_ordinal）
        block = (
            await session.execute(
                select(ParsedSourceBlockModel).where(
                    ParsedSourceBlockModel.parsed_source_id == parsed_id,
                    ParsedSourceBlockModel.ordinal == refs[0]["block_ordinal"],
                )
            )
        ).scalar_one()
        assert block.locator == refs[0]["locator"]
        # 完整证据链 ID 对齐
        assert card.company_id == env["company_id"]
        assert card.source_id == src
        assert card.parsed_source_id == parsed_id
        assert card.chunk_set_id == cs_id
        assert card.provider_key == "sse"
        assert card.authority_tier_snapshot == 1
        assert card.critical_claim_eligible_snapshot is True


# ---------------------------------------------------------------- provenance integrity


async def test_unknown_chunk_id_raises_provenance_integrity_error(env) -> None:
    chunk_id = uuid4()
    chunk_like = type("C", (), {"chunk_id": chunk_id})()
    draft = _draft(chunk_like)
    with pytest.raises(EvidenceProvenanceIntegrityError):
        await EvidenceCardService(env["sessionmaker"]).create_card(draft)


async def test_missing_chunk_row_raises_provenance_integrity_error(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM document_chunks WHERE chunk_id = :cid").bindparams(cid=chunk.chunk_id)
        )
        await session.commit()
    with pytest.raises(EvidenceProvenanceIntegrityError):
        await _create_card(env, chunk)


async def test_missing_chunk_set_row_raises_provenance_integrity_error(env) -> None:
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    async with env["sessionmaker"]() as session:
        # 删除 chunk_set 会级联删除 chunks（document_chunks FK CASCADE）。
        await session.execute(
            text("DELETE FROM chunk_sets WHERE chunk_set_id = :cid").bindparams(
                cid=chunk.chunk_set_id
            )
        )
        await session.commit()
    with pytest.raises(EvidenceProvenanceIntegrityError):
        await _create_card(env, chunk)


# ---------------------------------------------------------------- boundary


async def test_create_card_creates_no_stage5_report_tables(env) -> None:
    """Stage 边界：EvidenceCard 不产生 Stage 5 report 表。

    report_outlines / report_sections / reports / review_issues 是 Stage 5 表。
    Stage 4 claims 表由 Stage 4A 单独引入，不在这里约束（精确阶段边界名，
    避免以后过期）。
    """
    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    await _create_card(env, chunk)
    async with env["sessionmaker"]() as session:
        extra_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name IN ('report_outlines','report_sections',"
                    "'reports','review_issues')"
                )
            )
        ).scalar_one()
        assert extra_tables == 0
        # 0 vector manifest（不触发任何 index / Chroma 写入）。
        manifests = (
            await session.execute(text("SELECT count(*) FROM chunk_vector_indexes"))
        ).scalar_one()
        assert manifests == 0
    assert await _card_count(env["sessionmaker"]) == 1


async def test_service_takes_only_sessionmaker_no_llm_chroma(env) -> None:
    service = EvidenceCardService(env["sessionmaker"])
    # Service 只持有 sessionmaker：无 embedding / chroma / langgraph provider。
    assert set(service.__dict__) == {"_sessionmaker"}
    # EvidenceCard 不是 RetrievalHit 的自动升级：只有显式 create_card(draft)。
    assert not hasattr(service, "create_from_retrieval_hit")
    assert not hasattr(service, "create_from_hit")
