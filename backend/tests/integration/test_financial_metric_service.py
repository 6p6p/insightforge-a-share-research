"""FinancialMetricService integration tests (stage 4B.2A, spec N/O/P).

需要真实 PostgreSQL（127.0.0.1:5433）。Evidence 用真实 SourceRecord →
ParsedSource → ChunkingService → EvidenceCardService（document）与真实
MacroEvidenceService（macro，验证 origin 拒绝），**零 Chroma / 零 LLM /
零 Claim / 零 Report 表**。

覆盖：
- 创建：HTML / PDF metric Evidence → FinancialMetricObservation（字段 /
  raw_value / normalized_value_cny / period_kind / provenance 全链路）；
- 拒绝：evidence 缺失 / wrong company / non-document origin /
  non-metric evidence / value not found / value ambiguous；
- period 规则：balance → instant（period_start 必须 None）；income/cash-flow
  → duration（period_start 必须提供）；
- replay：同 fingerprint 复用同一行 / 并发 → 1；value / unit / period 变化 →
  新 observation，旧行保留；
- integrity：篡改 raw_value / normalized_value_cny → FinancialMetricIntegrityError，
  **不自动 repair**；EvidenceCard 行永远不被改写；
- 边界：不创建 Claim / Report；Service 只持有 sessionmaker。
"""

import asyncio
import hashlib
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.chunk_set import ChunkSetModel
from app.db.models.company import CompanyModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
    MacroEvidenceDraft,
)
from app.financial.contracts import (
    FinancialMetricDraft,
    MetricCode,
    RawUnit,
    StatementScope,
)
from app.financial.errors import (
    FinancialMetricEvidenceMismatch,
    FinancialMetricIntegrityError,
    FinancialMetricPeriodError,
    FinancialMetricScopeError,
    FinancialMetricStorageRangeError,
    FinancialMetricValueAmbiguous,
    FinancialMetricValueNotFound,
)
from app.financial.service import FinancialMetricService
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.macro_evidence_service import MacroEvidenceService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_macro_evidence_service import _seed_macro_chain

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "2024年贵州茅台营业收入情况？"
_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_SOURCE_TITLE = "财务新闻"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)

# 数值段落：'123,456' 出现 1 次。
_SINGLE_VALUE_P = "2024年贵州茅台营业收入123,456万元"
# 数值段落：'123,456' 出现 2 次（ambiguous）。
_AMBIGUOUS_P = "2024年营业收入为123,456万元，其中一至二季度为123,456万元"
# 数值段落：两个不同数值各 1 次（value change → 新 observation）。
_TWO_VALUES_P = "2024年营业收入为123,456万元，调整后为123,457万元"

_PDF_B1 = "贵州茅台2024年归属净利润862亿元"
_PDF_B2 = "丙丁" * 40

# Gate 0 A：exact numeric-token provenance 场景。
# '1000' 是完整 token，'100' / '000' 只是子串。
_PARTIAL_P = "2024年贵州茅台营业收入1000万元"
# '-123.45' 是完整 token（负号属于 token），'123.45' 不是。
_SIGN_P = "2024年贵州茅台净亏损-123.45万元"
# '(123.45)' 是完整 token（括号属于 token），'123.45' 不是。
_PAREN_P = "2024年贵州茅台净亏损(123.45)万元"
# 两个完整 '100' → ambiguous。
_AMBIG_100_P = "2024年营业收入100万元，调整后100万元"
# Gate 0 B：raw 合法（10^26 - 1）但 hundred_million_yuan normalize 后溢出。
_HUGE_NORMALIZED_P = "2024年贵州茅台营业收入99999999999999999999999999万元"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _fin_html(*paragraphs: str) -> bytes:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        "<html><head><title>财务新闻</title></head><body><article>"
        + body
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
        # Observation 先于 Evidence（source_evidence_card_id RESTRICT）。
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
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


async def _seed_other_company(sessionmaker) -> UUID:
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SZSE",
                security_code="000001",
                identity_key="SZSE:000001",
                board="szse_main",
                official_name="其他公司",
                short_name="其他",
                listing_status="listed",
                identity_source_provider_key="szse",
                identity_source_url="https://www.szse.cn",
            )
        )
        await session.commit()
    return company_id


async def _seed_html(env: dict, html_bytes: bytes) -> tuple:
    """真实 HTML：SourceRecord → ParsedSource(html_dom) → ChunkSet → Chunks。"""
    stored = env["raw_store"].put_html_bytes(html_bytes)
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
            reporting_period_end=None,
            source_url=_URL,
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsing = SourceParsingService(env["sessionmaker"], env["raw_store"])
    parsed = await parsing.parse_source(source_id)
    result = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(parsed.parsed_source_id)
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(result.chunk_set_id)
    assert chunks, "HTML seed must produce chunks"
    return source_id, parsed.parsed_source_id, result.chunk_set_id, chunks


async def _seed_pdf(env: dict) -> tuple:
    """手工 PDF：SourceRecord → ParsedSource(pdf_layout) + 2 blocks → ChunkSet。"""
    dummy_bytes = f"<html><body>pdf-seed {uuid4().hex}</body></html>".encode()
    stored = env["raw_store"].put_html_bytes(dummy_bytes)
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
            document_type="company_announcement",
            title="PDF标题",
            published_at=_PUBLISHED_AT,
            reporting_period_end=date(2024, 12, 31),
            source_url="https://www.sse.com.cn/2026/0809/0001.pdf",
            acquisition_method="user_upload",
            status="available",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            provider_capabilities_snapshot=["company_announcement"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
        artifact_id = artifact.artifact_id

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


async def _create_metric_card(
    env: dict,
    chunk,
    *,
    quote_start: int,
    quote_end: int,
    evidence_type: EvidenceType = EvidenceType.METRIC,
) -> dict:
    """创建 document EvidenceCard（quote 精确切片自 chunk.text）。"""
    result = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="营业收入为" + chunk.text[quote_start:quote_end] + "万元",
            evidence_type=evidence_type,
            chunk_id=chunk.chunk_id,
            quote_start=quote_start,
            quote_end=quote_end,
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    quote_text = chunk.text[quote_start:quote_end]
    return {"evidence_card_id": result.evidence_card_id, "quote_text": quote_text}


async def _card_for_value(env: dict, chunks, value: str) -> dict:
    """找到包含 value 的 chunk，quote 精确覆盖该 value 的 1 次出现。"""
    chunk = next(c for c in chunks if value in c.text)
    idx = chunk.text.index(value)
    return await _create_metric_card(env, chunk, quote_start=idx, quote_end=idx + len(value))


async def _card_spanning_value(env: dict, chunks, value: str) -> dict:
    """quote 覆盖 value 的**两次**出现（ambiguity 场景）。"""
    chunk = next(c for c in chunks if value in c.text)
    first = chunk.text.index(value)
    second = chunk.text.index(value, first + 1)
    return await _create_metric_card(env, chunk, quote_start=first, quote_end=second + len(value))


async def _card_for_context(env: dict, chunks, ctx: str) -> dict:
    """quote 精确覆盖 ctx（含上下文）在 chunk 文本中的一次出现。

    用于 Gate 0 numeric-token 场景：token 匹配必须基于完整引用上下文
    （"营业收入1000万元"），而不是孤立的数值子串。
    """
    chunk = next(c for c in chunks if ctx in c.text)
    idx = chunk.text.index(ctx)
    return await _create_metric_card(env, chunk, quote_start=idx, quote_end=idx + len(ctx))


def _obs_draft(
    env: dict,
    evidence_card_id: UUID,
    **overrides,
) -> FinancialMetricDraft:
    values = dict(
        company_id=env["company_id"],
        source_evidence_card_id=evidence_card_id,
        metric_code=MetricCode.REVENUE,
        statement_scope=StatementScope.CONSOLIDATED,
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        source_value_text="123,456",
        raw_unit=RawUnit.TEN_THOUSAND_YUAN,
    )
    values.update(overrides)
    return FinancialMetricDraft(**values)


def _service(env: dict) -> FinancialMetricService:
    return FinancialMetricService(env["sessionmaker"])


async def _obs_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM financial_metric_observations"))
            ).scalar_one()
        )


# ---------------------------------------------------------------- 创建


async def test_html_metric_observation_persists(env) -> None:
    """HTML metric Evidence → observation：字段 / raw_value / normalized / period。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    result = await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))
    assert result.replayed is False
    assert len(result.metric_fingerprint) == 64

    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                select(
                    text(
                        "metric_code, statement_scope, period_start, period_end, "
                        "period_kind, source_value_text, raw_value, raw_unit, "
                        "normalized_value_cny, metric_schema_version, "
                        "metric_fingerprint, company_id, source_evidence_card_id"
                    )
                )
                .select_from(text("financial_metric_observations"))
                .where(
                    text("metric_observation_id = :oid").bindparams(
                        oid=result.metric_observation_id
                    )
                )
            )
        ).one()
    assert row.metric_code == "revenue"
    assert row.statement_scope == "consolidated"
    assert row.period_start == date(2024, 1, 1)
    assert row.period_end == date(2024, 12, 31)
    assert row.period_kind == "duration"
    assert row.source_value_text == "123,456"
    assert row.raw_value == Decimal("123456")
    assert row.raw_unit == "ten_thousand_yuan"
    assert row.normalized_value_cny == Decimal("1234560000")
    assert row.metric_schema_version == 1
    assert row.metric_fingerprint == result.metric_fingerprint
    assert row.company_id == env["company_id"]
    assert row.source_evidence_card_id == card["evidence_card_id"]


async def test_pdf_metric_observation_persists(env) -> None:
    """PDF metric Evidence → observation（hundred_million_yuan 单位换算）。"""
    _, _, _, chunks = await _seed_pdf(env)
    card = await _card_for_value(env, chunks, "862")
    result = await _service(env).create_observation(
        _obs_draft(
            env,
            card["evidence_card_id"],
            metric_code=MetricCode.NET_PROFIT_PARENT,
            source_value_text="862",
            raw_unit=RawUnit.HUNDRED_MILLION_YUAN,
        )
    )
    assert result.replayed is False
    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                select(
                    text(
                        "raw_value, raw_unit, normalized_value_cny, period_kind, "
                        "metric_code, source_value_text"
                    )
                )
                .select_from(text("financial_metric_observations"))
                .where(
                    text("metric_observation_id = :oid").bindparams(
                        oid=result.metric_observation_id
                    )
                )
            )
        ).one()
    assert row.metric_code == "net_profit_parent"
    assert row.source_value_text == "862"
    assert row.raw_value == Decimal("862")
    assert row.raw_unit == "hundred_million_yuan"
    assert row.normalized_value_cny == Decimal("86200000000")
    assert row.period_kind == "duration"


# ---------------------------------------------------------------- 拒绝


async def test_missing_evidence_card_rejected(env) -> None:
    with pytest.raises(FinancialMetricEvidenceMismatch, match="not found"):
        await _service(env).create_observation(_obs_draft(env, uuid4()))


async def test_wrong_company_rejected(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    other_company = await _seed_other_company(env["sessionmaker"])
    with pytest.raises(FinancialMetricEvidenceMismatch, match="company"):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], company_id=other_company)
        )


async def test_non_document_origin_rejected(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    macro_card = await MacroEvidenceService(env["sessionmaker"]).create_macro_card(
        MacroEvidenceDraft(
            company_id=env["company_id"],
            research_question=_QUESTION,
            macro_observation_id=chain["observation_id"],
            evidence_statement="中国2024年GDP增速5.0%。",
            extractor_name="macro-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(macro_card.evidence_card_id)
    assert card is not None and card.origin_type == "macro_observation"
    with pytest.raises(FinancialMetricEvidenceMismatch, match="origin_type"):
        await _service(env).create_observation(_obs_draft(env, macro_card.evidence_card_id))


async def test_non_metric_evidence_rejected(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    fact_card = await _create_metric_card(
        env,
        next(c for c in chunks if "123,456" in c.text),
        quote_start=0,
        quote_end=8,
        evidence_type=EvidenceType.FACT,
    )
    with pytest.raises(FinancialMetricEvidenceMismatch, match="evidence_type"):
        await _service(env).create_observation(_obs_draft(env, fact_card["evidence_card_id"]))
    assert card["evidence_card_id"] != fact_card["evidence_card_id"]


async def test_value_not_found(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    with pytest.raises(FinancialMetricValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="999,999")
        )


async def test_value_ambiguous(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_AMBIGUOUS_P))
    card = await _card_spanning_value(env, chunks, "123,456")
    assert card["quote_text"].count("123,456") == 2
    with pytest.raises(FinancialMetricValueAmbiguous):
        await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))


# ---------------------------------------------------------------- Gate 0 A：exact numeric-token


async def test_value_exact_token_accepted(env) -> None:
    """quote "营业收入1000万元"：'1000' 是完整 token → 接受。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PARTIAL_P))
    card = await _card_for_context(env, chunks, "营业收入1000万元")
    assert card["quote_text"] == "营业收入1000万元"
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="1000")
    )
    assert result.replayed is False


async def test_value_partial_token_rejected(env) -> None:
    """quote "营业收入1000万元"：'100' 只是子串 → NotFound（禁止 partial match）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PARTIAL_P))
    card = await _card_for_context(env, chunks, "营业收入1000万元")
    with pytest.raises(FinancialMetricValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="100")
        )


async def test_value_partial_token_leading_zeros_rejected(env) -> None:
    """quote "营业收入1000万元"：'000' 只是子串 → NotFound。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PARTIAL_P))
    card = await _card_for_context(env, chunks, "营业收入1000万元")
    with pytest.raises(FinancialMetricValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="000")
        )


async def test_value_sign_belongs_to_token(env) -> None:
    """quote "净亏损-123.45万元"：'-123.45' 接受，剥掉符号的 '123.45' 拒绝。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SIGN_P))
    card = await _card_for_context(env, chunks, "净亏损-123.45万元")
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="-123.45")
    )
    assert result.replayed is False
    with pytest.raises(FinancialMetricValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="123.45")
        )


async def test_value_parenthesis_belongs_to_token(env) -> None:
    """quote "净亏损(123.45)万元"：'(123.45)' 接受，剥掉括号的 '123.45' 拒绝。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PAREN_P))
    card = await _card_for_context(env, chunks, "净亏损(123.45)万元")
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="(123.45)")
    )
    assert result.replayed is False
    with pytest.raises(FinancialMetricValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="123.45")
        )


async def test_duplicate_complete_tokens_ambiguous(env) -> None:
    """quote 中两个完整 '100' → source_value_text='100' 匹配 >1 → Ambiguous。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_AMBIG_100_P))
    card = await _card_for_context(env, chunks, "营业收入100万元，调整后100万元")
    with pytest.raises(FinancialMetricValueAmbiguous):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="100")
        )


# ---------------------------------------------------------------- Gate 0 B：NUMERIC(38,12) storage


async def test_storage_overflow_after_unit_normalize_rejected(env) -> None:
    """raw 合法（10^26 - 1）但 hundred_million_yuan normalize 后溢出 → 拒绝。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_HUGE_NORMALIZED_P))
    card = await _card_for_context(env, chunks, "营业收入99999999999999999999999999万元")
    with pytest.raises(FinancialMetricStorageRangeError):
        await _service(env).create_observation(
            _obs_draft(
                env,
                card["evidence_card_id"],
                source_value_text="99999999999999999999999999",
                raw_unit=RawUnit.HUNDRED_MILLION_YUAN,
            )
        )


# ---------------------------------------------------------------- Gate 0 B：metric/scope policy


async def test_net_profit_parent_consolidated_success(env) -> None:
    """net_profit_parent + consolidated → 成功（母公司指标的合并口径是白名单内）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    result = await _service(env).create_observation(
        _obs_draft(
            env,
            card["evidence_card_id"],
            metric_code=MetricCode.NET_PROFIT_PARENT,
            statement_scope=StatementScope.CONSOLIDATED,
        )
    )
    assert result.replayed is False


async def test_net_profit_parent_parent_scope_rejected(env) -> None:
    """net_profit_parent + parent → FinancialMetricScopeError。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    with pytest.raises(FinancialMetricScopeError):
        await _service(env).create_observation(
            _obs_draft(
                env,
                card["evidence_card_id"],
                metric_code=MetricCode.NET_PROFIT_PARENT,
                statement_scope=StatementScope.PARENT,
            )
        )


async def test_net_profit_parent_excl_nonrecurring_parent_scope_rejected(env) -> None:
    """net_profit_parent_excl_nonrecurring + parent → FinancialMetricScopeError。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    with pytest.raises(FinancialMetricScopeError):
        await _service(env).create_observation(
            _obs_draft(
                env,
                card["evidence_card_id"],
                metric_code=MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING,
                statement_scope=StatementScope.PARENT,
            )
        )


async def test_equity_parent_parent_scope_rejected(env) -> None:
    """equity_parent + parent → FinancialMetricScopeError（balance sheet instant）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    with pytest.raises(FinancialMetricScopeError):
        await _service(env).create_observation(
            _obs_draft(
                env,
                card["evidence_card_id"],
                metric_code=MetricCode.EQUITY_PARENT,
                statement_scope=StatementScope.PARENT,
                period_start=None,
                period_end=date(2024, 12, 31),
            )
        )


async def test_revenue_both_scopes_structurally_allowed(env) -> None:
    """revenue 不在 consolidated-only 白名单 → consolidated / parent 都 structurally 允许。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    cons = await svc.create_observation(
        _obs_draft(env, card["evidence_card_id"], statement_scope=StatementScope.CONSOLIDATED)
    )
    parent = await svc.create_observation(
        _obs_draft(env, card["evidence_card_id"], statement_scope=StatementScope.PARENT)
    )
    assert cons.metric_observation_id != parent.metric_observation_id


# ---------------------------------------------------------------- period 规则


async def test_balance_sheet_instant_period(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    result = await _service(env).create_observation(
        _obs_draft(
            env,
            card["evidence_card_id"],
            metric_code=MetricCode.TOTAL_ASSETS,
            period_start=None,
            period_end=date(2024, 12, 31),
        )
    )
    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                select(text("period_kind, period_start, period_end"))
                .select_from(text("financial_metric_observations"))
                .where(
                    text("metric_observation_id = :oid").bindparams(
                        oid=result.metric_observation_id
                    )
                )
            )
        ).one()
    assert row.period_kind == "instant"
    assert row.period_start is None
    assert row.period_end == date(2024, 12, 31)


async def test_balance_sheet_period_start_rejected(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    with pytest.raises(FinancialMetricPeriodError):
        await _service(env).create_observation(
            _obs_draft(
                env,
                card["evidence_card_id"],
                metric_code=MetricCode.TOTAL_ASSETS,
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
            )
        )


async def test_duration_requires_period_start(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    with pytest.raises(FinancialMetricPeriodError):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], period_start=None)
        )


# ---------------------------------------------------------------- replay


async def test_replay_returns_same_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    first = await svc.create_observation(_obs_draft(env, card["evidence_card_id"]))
    second = await svc.create_observation(_obs_draft(env, card["evidence_card_id"]))
    assert first.replayed is False
    assert second.replayed is True
    assert second.metric_observation_id == first.metric_observation_id
    assert second.metric_fingerprint == first.metric_fingerprint
    assert await _obs_count(env["sessionmaker"]) == 1


async def test_concurrent_create_single_row(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    results = await asyncio.gather(svc.create_observation(draft), svc.create_observation(draft))
    ids = {r.metric_observation_id for r in results}
    assert len(ids) == 1
    assert await _obs_count(env["sessionmaker"]) == 1


async def test_value_change_creates_new_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_TWO_VALUES_P))
    card_a = await _card_for_value(env, chunks, "123,456")
    card_b = await _card_for_value(env, chunks, "123,457")
    svc = _service(env)
    a = await svc.create_observation(_obs_draft(env, card_a["evidence_card_id"]))
    b = await svc.create_observation(
        _obs_draft(env, card_b["evidence_card_id"], source_value_text="123,457")
    )
    assert a.metric_observation_id != b.metric_observation_id
    assert await _obs_count(env["sessionmaker"]) == 2


async def test_unit_change_creates_new_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    yuan = await svc.create_observation(
        _obs_draft(env, card["evidence_card_id"], raw_unit=RawUnit.YUAN)
    )
    ten_thousand = await svc.create_observation(
        _obs_draft(env, card["evidence_card_id"], raw_unit=RawUnit.TEN_THOUSAND_YUAN)
    )
    assert yuan.metric_observation_id != ten_thousand.metric_observation_id
    async with env["sessionmaker"]() as session:
        normalized = (
            await session.execute(
                select(text("normalized_value_cny, raw_unit"))
                .select_from(text("financial_metric_observations"))
                .where(text("metric_fingerprint = :fp").bindparams(fp=yuan.metric_fingerprint))
            )
        ).one()
    assert normalized.normalized_value_cny == Decimal("123456")  # yuan
    assert await _obs_count(env["sessionmaker"]) == 2


async def test_period_change_creates_new_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    fy2024 = await svc.create_observation(_obs_draft(env, card["evidence_card_id"]))
    fy2023 = await svc.create_observation(
        _obs_draft(
            env,
            card["evidence_card_id"],
            period_start=date(2023, 1, 1),
            period_end=date(2023, 12, 31),
        )
    )
    assert fy2024.metric_observation_id != fy2023.metric_observation_id
    assert await _obs_count(env["sessionmaker"]) == 2


async def test_replay_corruption_raises_integrity_error(env) -> None:
    """篡改已落库 raw_value → replay 校验发现损坏 → IntegrityError（不 repair）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    first = await svc.create_observation(draft)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_metric_observations SET raw_value = raw_value + 1 "
                "WHERE metric_fingerprint = :fp"
            ).bindparams(fp=first.metric_fingerprint)
        )
        await session.commit()

    with pytest.raises(FinancialMetricIntegrityError, match="raw_value"):
        await svc.create_observation(draft)


async def test_replay_corruption_normalized_raises_integrity_error(env) -> None:
    """篡改 normalized_value_cny → IntegrityError。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    first = await svc.create_observation(draft)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_metric_observations "
                "SET normalized_value_cny = normalized_value_cny + 1 "
                "WHERE metric_fingerprint = :fp"
            ).bindparams(fp=first.metric_fingerprint)
        )
        await session.commit()

    with pytest.raises(FinancialMetricIntegrityError, match="normalized_value_cny"):
        await svc.create_observation(draft)


async def test_evidence_card_unchanged(env) -> None:
    """create_observation 永远不改写 EvidenceCard 行。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    async with env["sessionmaker"]() as session:
        before = await EvidenceCardRepository(session).get_by_id(card["evidence_card_id"])
        before_fp = before.evidence_fingerprint
        before_quote = before.quote_text
    await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))
    async with env["sessionmaker"]() as session:
        after = await EvidenceCardRepository(session).get_by_id(card["evidence_card_id"])
        assert after.evidence_fingerprint == before_fp
        assert after.quote_text == before_quote
        assert after.evidence_statement == before.evidence_statement
        assert after.created_at == before.created_at


async def test_provenance_traces_to_raw_artifact(env) -> None:
    """Observation → EvidenceCard → Chunk → ParsedSource → SourceRecord → RawArtifact。"""
    _, parsed_id, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    result = await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))

    async with env["sessionmaker"]() as session:
        obs = (
            await session.execute(
                select(text("source_evidence_card_id, company_id"))
                .select_from(text("financial_metric_observations"))
                .where(
                    text("metric_observation_id = :oid").bindparams(
                        oid=result.metric_observation_id
                    )
                )
            )
        ).one()
        evidence = await EvidenceCardRepository(session).get_by_id(obs.source_evidence_card_id)
        assert evidence is not None
        chunk = await session.get(DocumentChunkModel, evidence.chunk_id)
        assert chunk is not None
        chunk_set = await session.get(ChunkSetModel, chunk.chunk_set_id)
        assert chunk_set is not None and chunk_set.parsed_source_id == parsed_id
        parsed = (
            await session.execute(
                select(text("source_id, artifact_id"))
                .select_from(text("parsed_sources"))
                .where(text("parsed_source_id = :pid").bindparams(pid=parsed_id))
            )
        ).one()
        source = await SourceRecordRepository(session).get_by_id(parsed.source_id)
        assert source is not None
        artifact = await RawArtifactRepository(session).get_by_id(parsed.artifact_id)
        assert artifact is not None
        assert source.artifact_id == artifact.artifact_id
        assert obs.company_id == evidence.company_id == source.company_id


# ---------------------------------------------------------------- 边界


async def test_no_claims_or_stage5_report_tables(env) -> None:
    """create_observation 不创建 Claim / Report / ReviewIssue（Stage 4B.2A 边界）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "123,456")
    await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))

    async with env["sessionmaker"]() as session:
        claim_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name IN ('claims', 'claim_evidence_links')"
                )
            )
        ).scalar_one()
        stage5_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('report_sections','review_issues')"
                )
            )
        ).scalar_one()
    assert claim_tables == 2
    assert stage5_tables == 0
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


async def test_service_takes_only_sessionmaker(env) -> None:
    """Service 只持有 sessionmaker：无 LLM / LangGraph / Chroma / Report provider。"""
    service = FinancialMetricService(env["sessionmaker"])
    assert set(service.__dict__) == {"_sessionmaker"}
