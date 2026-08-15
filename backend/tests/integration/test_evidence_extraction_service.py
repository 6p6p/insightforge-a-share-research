"""EvidenceExtractionService integration tests (stage 3C.2, spec 12/13).

需要真实 PostgreSQL（127.0.0.1:5433）。复用 3C.1 的 `env` fixture 与
seed helpers：HTML 用真实 SourceParsingService + ChunkingService；PDF 用手工
seed ParsedSource + pdf_page locator blocks。Extractor 一律使用
FakeEvidenceExtractionModel（零真实 LLM / 零 Chroma / 零网络）。

覆盖：
- E2E HTML：extract_from_hit → EvidenceCard，quote 精确切片 + DOM locator，
  extractor_name/version/model_id/confidence 落库，provenance 快照；
- E2E HTML 跨 block：quote 跨 "\n" → 2 个 DOM locator；
- E2E PDF：跨 page/bbox 的 quote → 2 个 locator_refs，回溯到 ParsedSourceBlock；
- rerun → replay（同 fingerprint 复用，不重复创建）；
- stale（DB 变更 chunk.text）→ EvidenceExtractionInputStale，0 写入，LLM 未调用；
- relevant=false / quote not found / ambiguous / malformed → 0 写入；
- high confidence 不提升 critical snapshot；authority/critical 复制 SourceRecord；
- 0 manifest：不创建 chunk_vector_indexes；不写 claims/reports/report_audits/
  review_issues 行。
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EVIDENCE_EXTRACTOR_NAME,
    EVIDENCE_EXTRACTOR_VERSION,
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.evidence.extractor.errors import (
    EvidenceExtractionInputStale,
    EvidenceExtractionQuoteAmbiguous,
    EvidenceExtractionQuoteNotFound,
)
from app.evidence.extractor.service import EvidenceExtractionService
from app.rag.retrieval.contracts import RetrievalHit
from app.repositories.company_repository import CompanyRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.test_evidence_card_service import (
    _PDF_B1,
    _PDF_B2,
    _seed_html_source,
    _seed_pdf_source,
    _sha,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


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


_QUESTION = "2024年贵州茅台净利润增长情况？"
_SOURCE_TITLE = "新闻标题"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_PERIOD_END = date(2024, 12, 31)

_FAKE_MODEL_ID = "fake/structured-llm@1"


def _item(
    *,
    statement: str,
    quote: str,
    confidence: str = "high",
    evidence_type: str = "metric",
) -> EvidenceExtractionItem:
    return EvidenceExtractionItem(
        evidence_statement=statement,
        evidence_type=EvidenceType(evidence_type),
        quote_text=quote,
        confidence=EvidenceConfidence(confidence),
    )


def _hit_for_chunk(
    chunk,
    *,
    source_id,
    parsed_source_id,
    company_id,
    provider_key="xinhuanet",
    document_type="news_article",
    authority_tier=3,
    critical_claim_eligible=False,
    published_at=_PUBLISHED_AT,
    reporting_period_end=None,
) -> RetrievalHit:
    return RetrievalHit(
        rank=1,
        chunk_id=chunk.chunk_id,
        chunk_set_id=chunk.chunk_set_id,
        parsed_source_id=parsed_source_id,
        source_id=source_id,
        company_id=company_id,
        text=chunk.text,
        distance=0.1,
        provider_key=provider_key,
        document_type=document_type,
        source_title=_SOURCE_TITLE,
        source_url=None,
        published_at=published_at,
        reporting_period_end=reporting_period_end,
        authority_tier=authority_tier,
        critical_claim_eligible=critical_claim_eligible,
        chunk_ordinal=chunk.ordinal,
        locator_refs=chunk.locator_refs,
    )


async def _card_by_id(sessionmaker, card_id):
    async with sessionmaker() as session:
        return await EvidenceCardRepository(session).get_by_id(card_id)


async def _card_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM evidence_cards"))).scalar_one()
        )


# ---------------------------------------------------------------- E2E HTML


async def test_e2e_html_extraction_creates_card_with_dom_locator(env) -> None:
    src, parsed_id, cs_id, chunks = await _seed_html_source(env)
    chunk = chunks[0]  # "甲"*200 + "\n" + "乙"*200（401 字）
    # 唯一 quote 必须跨 "\n"（纯单字串在 200 字重复块内是重叠重复 → ambiguous）。
    quote = chunk.text[195:211]  # block1 末尾 5 字 + "\n" + block2 前 10 字
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="公司营业收入为100亿元。", quote=quote, confidence="high")],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)

    assert result.relevant is True
    assert result.reason_code is None
    assert result.created_count == 1
    assert result.replayed_count == 0
    assert len(result.evidence_card_ids) == 1

    card = await _card_by_id(env["sessionmaker"], result.evidence_card_ids[0])
    assert card is not None
    # extractor 身份 + 置信度落库
    assert card.extractor_name == EVIDENCE_EXTRACTOR_NAME
    assert card.extractor_version == EVIDENCE_EXTRACTOR_VERSION
    assert card.extractor_model_id == _FAKE_MODEL_ID
    assert card.extractor_confidence == "high"
    # quote 精确切片 + sha256（跨 block：block1 末尾 5 字 + block2 前 10 字）
    assert card.quote_start == 195
    assert card.quote_end == 211
    assert card.quote_text == chunk.text[195:211]
    assert card.quote_sha256 == _sha(chunk.text[195:211])
    assert card.research_question == _QUESTION
    assert card.research_question_sha256 == _sha(_QUESTION)
    # quote 级 DOM locator：跨 block → 2 个
    assert len(card.locator_refs) == 2
    refs = card.locator_refs
    assert refs[0]["block_ordinal"] == 1
    assert refs[0]["char_start"] == 195
    assert refs[0]["char_end"] == 200
    assert refs[0]["locator"]["type"] == "html_dom"
    assert refs[0]["locator"]["xpath"].startswith("/html/body/article/p[")
    assert refs[1]["block_ordinal"] == 2
    assert refs[1]["char_start"] == 0
    assert refs[1]["char_end"] == 10
    assert refs[1]["locator"]["type"] == "html_dom"
    # provenance 快照（复制 SourceRecord，非 hit 输入）
    assert card.company_id == env["company_id"]
    assert card.source_id == src
    assert card.parsed_source_id == parsed_id
    assert card.chunk_set_id == cs_id
    assert card.chunk_id == chunk.chunk_id
    assert card.provider_key == "xinhuanet"
    assert card.authority_tier_snapshot == 3
    assert card.critical_claim_eligible_snapshot is False


# ---------------------------------------------------------------- E2E PDF


async def test_e2e_pdf_extraction_cross_page_quote_keeps_page_bbox_locators(env) -> None:
    src, parsed_id, cs_id, chunks = await _seed_pdf_source(env)
    chunk = chunks[0]
    assert chunk.text == _PDF_B1 + "\n" + _PDF_B2
    # quote 跨 block：block1 末尾 "营业收入" + "\n" + block2 全文。
    quote = "营业收入\n" + _PDF_B2
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="2024年贵州茅台归属净利润862亿元。", quote=quote)],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
        provider_key="sse",
        document_type="company_announcement",
        authority_tier=1,
        critical_claim_eligible=True,
        reporting_period_end=_PERIOD_END,
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)
    assert result.created_count == 1

    card = await _card_by_id(env["sessionmaker"], result.evidence_card_ids[0])
    assert card is not None
    assert card.quote_text == quote
    assert card.quote_sha256 == _sha(quote)
    # page/bbox locator 原样保留 + 缩窄到原 block 字符范围
    assert len(card.locator_refs) == 2
    refs = card.locator_refs
    assert refs[0]["block_ordinal"] == 1
    assert refs[0]["char_start"] == 9
    assert refs[0]["char_end"] == 13
    assert refs[0]["locator"]["type"] == "pdf_page"
    assert refs[0]["locator"]["page_number"] == 1
    assert refs[0]["locator"]["bbox"] == [50.0, 100.0, 200.0, 120.0]
    assert refs[1]["block_ordinal"] == 2
    assert refs[1]["char_start"] == 0
    assert refs[1]["char_end"] == 10
    assert refs[1]["locator"]["page_number"] == 2
    assert refs[1]["locator"]["bbox"] == [30.0, 80.0, 300.0, 100.0]
    # 回溯到 ParsedSourceBlock
    async with env["sessionmaker"]() as session:
        block = (
            await session.execute(
                text(
                    "SELECT locator FROM parsed_source_blocks "
                    "WHERE parsed_source_id = :pid AND ordinal = :ord"
                ).bindparams(pid=parsed_id, ord=refs[0]["block_ordinal"])
            )
        ).scalar_one()
    assert block == refs[0]["locator"]
    # provenance 快照（PDF seed：authority 1 / critical True / sse）
    assert card.provider_key == "sse"
    assert card.authority_tier_snapshot == 1
    assert card.critical_claim_eligible_snapshot is True
    assert card.reporting_period_end == _PERIOD_END
    assert card.source_id == src
    assert card.parsed_source_id == parsed_id
    assert card.chunk_set_id == cs_id


# ---------------------------------------------------------------- replay


async def test_rerun_replays_same_card(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    quote = chunk.text[195:211]  # 唯一（跨 "\n"）
    decision = EvidenceExtractionDecision(
        relevant=True,
        items=[_item(statement="营业收入为100亿元。", quote=quote)],
    )
    fake = FakeEvidenceExtractionModel(decision=decision)
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)

    first = await service.extract_from_hit(_QUESTION, hit)
    second = await service.extract_from_hit(_QUESTION, hit)
    assert first.created_count == 1
    assert first.replayed_count == 0
    assert second.created_count == 0
    assert second.replayed_count == 1
    assert first.evidence_card_ids == second.evidence_card_ids
    assert await _card_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- stale guard


async def test_stale_hit_text_rejected_zero_writes(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE document_chunks SET text = :new_text, "
                "text_sha256 = :new_sha, char_count = :n WHERE chunk_id = :cid"
            ).bindparams(
                new_text="已变更文本",
                new_sha=_sha("已变更文本"),
                n=5,
                cid=chunk.chunk_id,
            )
        )
        await session.commit()
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="营业收入为100亿元。", quote=chunk.text[195:211])],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    with pytest.raises(EvidenceExtractionInputStale):
        await service.extract_from_hit(_QUESTION, hit)
    assert fake.calls == []  # stale 在 LLM 调用前拒绝
    assert await _card_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- zero-write paths


async def test_relevant_false_zero_writes(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=False,
            items=[],
            reason_code=EvidenceExtractionReason.NOT_RELEVANT,
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)
    assert result.relevant is False
    assert result.evidence_card_ids == []
    assert result.reason_code == EvidenceExtractionReason.NOT_RELEVANT
    assert await _card_count(env["sessionmaker"]) == 0


async def test_quote_not_found_zero_writes(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="不存在。", quote="完全不存在的文本")],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        await service.extract_from_hit(_QUESTION, hit)
    assert await _card_count(env["sessionmaker"]) == 0


async def test_quote_ambiguous_zero_writes(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]  # "甲"*200 → 单字 "甲" 出现 200 次
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="单字引用。", quote="甲")],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    with pytest.raises(EvidenceExtractionQuoteAmbiguous):
        await service.extract_from_hit(_QUESTION, hit)
    assert await _card_count(env["sessionmaker"]) == 0


async def test_relevant_true_zero_items_yields_no_cards(env) -> None:
    """v3（V1.1 closure）：relevant=true + items=[] 合法——「相关但无原子证据」，
    生产实测模型高频输出该形态；无卡创建（等同无证据，不抛错）。"""
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    fake = FakeEvidenceExtractionModel(decision={"relevant": True, "items": []})
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)
    assert result.relevant is True
    assert result.created_count == 0
    assert await _card_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- authority / critical


async def test_high_confidence_does_not_promote_critical_snapshot(env) -> None:
    # SourceRecord critical_claim_eligible=False：即使 extractor_confidence=high
    # 也不得自动提升（快照直接复制 SourceRecord）。
    src, parsed_id, _, chunks = await _seed_html_source(env, critical_claim_eligible=False)
    chunk = chunks[0]
    quote = chunk.text[195:211]  # 唯一（跨 "\n"）
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="营业收入为100亿元。", quote=quote, confidence="high")],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)
    card = await _card_by_id(env["sessionmaker"], result.evidence_card_ids[0])
    assert card is not None
    assert card.extractor_confidence == "high"
    assert card.critical_claim_eligible_snapshot is False


# ---------------------------------------------------------------- boundary


async def test_single_hit_three_items_creates_three_cards(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]  # "甲"*200 + "\n" + "乙"*200
    # 三个唯一 quote：都包含唯一分隔符 "\n"（纯单字串在重复块内是重叠重复）。
    quote_a = "甲" + "\n" + "乙"  # chunk[199:202]
    quote_b = "甲" * 5 + "\n"  # chunk[195:201]
    quote_c = "\n" + "乙" * 10  # chunk[200:211]
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[
                _item(statement="甲段。", quote=quote_a),
                _item(statement="乙段。", quote=quote_b),
                _item(statement="甲段更长。", quote=quote_c),
            ],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)
    assert result.created_count == 3
    assert len(result.evidence_card_ids) == 3
    assert await _card_count(env["sessionmaker"]) == 3


async def test_zero_chroma_and_no_stage5_report_tables(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    quote = chunk.text[195:211]  # 唯一（跨 "\n"）
    fake = FakeEvidenceExtractionModel(
        decision=EvidenceExtractionDecision(
            relevant=True,
            items=[_item(statement="营业收入为100亿元。", quote=quote)],
        )
    )
    hit = _hit_for_chunk(
        chunk,
        source_id=src,
        parsed_source_id=parsed_id,
        company_id=env["company_id"],
    )
    service = EvidenceExtractionService(env["sessionmaker"], model=fake)
    result = await service.extract_from_hit(_QUESTION, hit)
    assert result.created_count == 1
    assert len(fake.calls) == 1  # LLM 侧只有 fake（零真实 provider / 零网络）

    async with env["sessionmaker"]() as session:
        # 0 vector manifest（不触发任何 Chroma index 写入）。
        manifests = (
            await session.execute(text("SELECT count(*) FROM chunk_vector_indexes"))
        ).scalar_one()
        assert manifests == 0
        # Stage 边界：Stage 3 extraction 不产生未来阶段（5E+）表；Stage 5A-5D
        # 表（report_outlines / draft_sections / reports / report_check_results /
        # report_audits / review_issues，migration 0032-0035）已存在但本阶段不写行。
        # Stage 4 claims 表由 Stage 4A 单独引入，不在这里约束
        # （精确阶段边界名，避免以后过期）。
        extra = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name IN ('report_sections')"
                )
            )
        ).scalar_one()
        assert extra == 0
        # Stage 5A/5B/5C 表已存在（migration 0032/0033/0034），但本阶段不写行。
        outline_rows = (
            await session.execute(text("SELECT count(*) FROM report_outlines"))
        ).scalar_one()
        assert int(outline_rows) == 0
        report_rows = (await session.execute(text("SELECT count(*) FROM reports"))).scalar_one()
        assert int(report_rows) == 0
        check_rows = (
            await session.execute(text("SELECT count(*) FROM report_check_results"))
        ).scalar_one()
        assert int(check_rows) == 0
        # Stage 5D 的 report_audits / review_issues（migration 0035）已存在，
        # 但本阶段不写行。
        audit_rows = (
            await session.execute(text("SELECT count(*) FROM report_audits"))
        ).scalar_one()
        assert int(audit_rows) == 0
        issue_rows = (
            await session.execute(text("SELECT count(*) FROM review_issues"))
        ).scalar_one()
        assert int(issue_rows) == 0
    assert await _card_count(env["sessionmaker"]) == 1
