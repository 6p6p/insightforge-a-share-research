"""ValuationObservationService integration tests (stage 4C.2A, spec K/L/M).

需要真实 PostgreSQL（127.0.0.1:5433）。Evidence 用真实 SourceRecord →
ParsedSource → ChunkingService → EvidenceCardService（document，metric）；
macro origin 拒绝用真实 MacroEvidenceService 链。**零 Chroma / 零 LLM /
零 Claim / 零 Report 表**。

覆盖（spec Z 的 observation 部分）：
- 创建：HTML metric Evidence → ValuationMetricObservation（字段 / metric_value /
  provenance 全链路）；
- 拒绝：evidence 缺失 / wrong company / non-document origin / non-metric
  evidence / value not found / value ambiguous / value not numeric /
  NUMERIC(38,12) 溢出；
- exact numeric-token（spec K）：'1000' 是完整 token，'100' / '000' 不是；
  '-123.45' / '(123.45)' 的符号与括号属于 token；
- replay（spec W）：同 fingerprint 复用同一行 / 并发 → 1；value / metric_code /
  metric_as_of / evidence 变化 → 新 observation，旧行保留；
- integrity：篡改 metric_value / source_value_text → ValuationIntegrityError，
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
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
    MacroEvidenceDraft,
)
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
from app.valuation.contracts import ValuationMetricCode, ValuationMetricDraft
from app.valuation.errors import (
    ValuationIntegrityError,
    ValuationObservationEvidenceMismatch,
    ValuationStorageRangeError,
    ValuationValueAmbiguous,
    ValuationValueNotFound,
    ValuationValueNotNumeric,
)
from app.valuation.observation_service import ValuationObservationService
from tests.integration.test_macro_evidence_service import _seed_macro_chain

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台当前市盈率水平？"
_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_SOURCE_TITLE = "估值新闻"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_METRIC_AS_OF = date(2026, 8, 7)

# 单值段落：'15.3' 出现 1 次。
_SINGLE_VALUE_P = "2026年公司市盈率15.3倍"
# 数值段落：'15.3' 出现 2 次（ambiguous）。
_AMBIGUOUS_P = "2026年市盈率为15.3倍，同业平均为15.3倍"
# 两个不同数值各 1 次（value change → 新 observation）。
_TWO_VALUES_P = "2026年市盈率为15.3倍，调整后为15.4倍"

# exact numeric-token 场景。
_PARTIAL_P = "2026年公司市盈率1000倍"
_SIGN_P = "2026年公司市盈率-123.45倍"
_PAREN_P = "2026年公司市盈率(123.45)倍"
_AMBIG_100_P = "2026年市盈率100倍，同业市盈率100倍"

# NUMERIC(38,12) 溢出：13 位小数。
_OVERFLOW_P = "2026年公司市盈率123456789.1234567890123倍"

# 非数字文本（单位内嵌 → ValuationValueNotNumeric）。
_NOT_NUMERIC_P = "2026年公司市盈率100万元"


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _fin_html(*paragraphs: str) -> bytes:
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        "<html><head><title>估值新闻</title></head><body><article>"
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
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
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


async def _create_metric_card(
    env: dict,
    chunk,
    *,
    quote_start: int,
    quote_end: int,
    evidence_type: EvidenceType = EvidenceType.METRIC,
) -> dict:
    result = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="市盈率为" + chunk.text[quote_start:quote_end] + "倍",
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
    """quote 精确覆盖 ctx（含上下文）在 chunk 文本中的一次出现。"""
    chunk = next(c for c in chunks if ctx in c.text)
    idx = chunk.text.index(ctx)
    return await _create_metric_card(env, chunk, quote_start=idx, quote_end=idx + len(ctx))


def _obs_draft(env: dict, evidence_card_id: UUID, **overrides) -> ValuationMetricDraft:
    values = dict(
        company_id=env["company_id"],
        source_evidence_card_id=evidence_card_id,
        metric_code=ValuationMetricCode.PE_TTM,
        metric_as_of=_METRIC_AS_OF,
        source_value_text="15.3",
    )
    values.update(overrides)
    return ValuationMetricDraft(**values)


def _service(env: dict) -> ValuationObservationService:
    return ValuationObservationService(env["sessionmaker"])


async def _obs_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM valuation_metric_observations"))
            ).scalar_one()
        )


# ---------------------------------------------------------------- 创建


async def test_html_metric_observation_persists(env) -> None:
    """HTML metric Evidence → observation：字段 / metric_value / provenance。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    result = await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))
    assert result.replayed is False
    assert len(result.valuation_observation_fingerprint) == 64

    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                select(
                    text(
                        "metric_code, metric_as_of, source_value_text, metric_value, "
                        "valuation_observation_schema_version, "
                        "valuation_observation_fingerprint, company_id, "
                        "source_evidence_card_id"
                    )
                )
                .select_from(text("valuation_metric_observations"))
                .where(
                    text("valuation_observation_id = :oid").bindparams(
                        oid=result.valuation_observation_id
                    )
                )
            )
        ).one()
    assert row.metric_code == "pe_ttm"
    assert row.metric_as_of == _METRIC_AS_OF
    assert row.source_value_text == "15.3"
    assert row.metric_value == Decimal("15.3")
    assert row.valuation_observation_schema_version == 1
    assert row.valuation_observation_fingerprint == result.valuation_observation_fingerprint
    assert row.company_id == env["company_id"]
    assert row.source_evidence_card_id == card["evidence_card_id"]


async def test_metric_value_deterministic_decimal(env) -> None:
    """metric_value 完全由 source_value_text 解析（千分位 / 小数 / 正负号）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html("2026年公司市盈率1,234.5倍"))
    card = await _card_for_value(env, chunks, "1,234.5")
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="1,234.5")
    )
    async with env["sessionmaker"]() as session:
        value = (
            await session.execute(
                select(text("metric_value"))
                .select_from(text("valuation_metric_observations"))
                .where(
                    text("valuation_observation_id = :oid").bindparams(
                        oid=result.valuation_observation_id
                    )
                )
            )
        ).scalar_one()
    assert value == Decimal("1234.5")


# ---------------------------------------------------------------- 拒绝


async def test_missing_evidence_card_rejected(env) -> None:
    with pytest.raises(ValuationObservationEvidenceMismatch, match="not found"):
        await _service(env).create_observation(_obs_draft(env, uuid4()))


async def test_wrong_company_rejected(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    other_company = await _seed_other_company(env["sessionmaker"])
    with pytest.raises(ValuationObservationEvidenceMismatch, match="company"):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], company_id=other_company)
        )


async def test_non_document_origin_rejected(env, monkeypatch) -> None:
    """origin_type=macro_observation 的 Evidence → 拒绝（spec K：document_chunk only）。"""
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
    with pytest.raises(ValuationObservationEvidenceMismatch, match="origin_type"):
        await _service(env).create_observation(_obs_draft(env, macro_card.evidence_card_id))


async def test_non_metric_evidence_rejected(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    chunk = next(c for c in chunks if "15.3" in c.text)
    fact_card = await _create_metric_card(
        env, chunk, quote_start=0, quote_end=8, evidence_type=EvidenceType.FACT
    )
    with pytest.raises(ValuationObservationEvidenceMismatch, match="evidence_type"):
        await _service(env).create_observation(_obs_draft(env, fact_card["evidence_card_id"]))


async def test_value_not_found(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    with pytest.raises(ValuationValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="99.9")
        )


async def test_value_ambiguous(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_AMBIGUOUS_P))
    card = await _card_spanning_value(env, chunks, "15.3")
    assert card["quote_text"].count("15.3") == 2
    with pytest.raises(ValuationValueAmbiguous):
        await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))


async def test_value_not_numeric_rejected(env) -> None:
    """'100万'（单位内嵌）不是合法数字 token → ValuationValueNotNumeric。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_NOT_NUMERIC_P))
    card = await _card_for_context(env, chunks, "市盈率100万元")
    with pytest.raises(ValuationValueNotNumeric):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="100万")
        )


async def test_storage_overflow_rejected(env) -> None:
    """13 位小数超出 NUMERIC(38,12) → ValuationStorageRangeError（禁止静默截断）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_OVERFLOW_P))
    card = await _card_for_context(env, chunks, "市盈率123456789.1234567890123倍")
    with pytest.raises(ValuationStorageRangeError):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="123456789.1234567890123")
        )


# ---------------------------------------------------------------- exact numeric-token


async def test_value_exact_token_accepted(env) -> None:
    """quote "市盈率1000倍"：'1000' 是完整 token → 接受。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PARTIAL_P))
    card = await _card_for_context(env, chunks, "市盈率1000倍")
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="1000")
    )
    assert result.replayed is False


async def test_value_partial_token_rejected(env) -> None:
    """quote "市盈率1000倍"：'100' 只是子串 → NotFound（禁止 partial match）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PARTIAL_P))
    card = await _card_for_context(env, chunks, "市盈率1000倍")
    with pytest.raises(ValuationValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="100")
        )


async def test_value_partial_token_leading_zeros_rejected(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_PARTIAL_P))
    card = await _card_for_context(env, chunks, "市盈率1000倍")
    with pytest.raises(ValuationValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="000")
        )


async def test_value_sign_belongs_to_token(env) -> None:
    """quote "市盈率-123.45倍"：'-123.45' 接受，剥掉符号的 '123.45' 拒绝。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SIGN_P))
    card = await _card_for_context(env, chunks, "市盈率-123.45倍")
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="-123.45")
    )
    assert result.replayed is False
    with pytest.raises(ValuationValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="123.45")
        )


async def test_value_parenthesis_belongs_to_token(env) -> None:
    """quote "市盈率(123.45)倍"：'(123.45)' 接受，剥掉括号的 '123.45' 拒绝。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_PAREN_P))
    card = await _card_for_context(env, chunks, "市盈率(123.45)倍")
    result = await _service(env).create_observation(
        _obs_draft(env, card["evidence_card_id"], source_value_text="(123.45)")
    )
    assert result.replayed is False
    with pytest.raises(ValuationValueNotFound):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="123.45")
        )


async def test_duplicate_complete_tokens_ambiguous(env) -> None:
    """quote 中两个完整 '100' → source_value_text='100' 匹配 >1 → Ambiguous。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_AMBIG_100_P))
    card = await _card_for_context(env, chunks, "市盈率100倍，同业市盈率100倍")
    with pytest.raises(ValuationValueAmbiguous):
        await _service(env).create_observation(
            _obs_draft(env, card["evidence_card_id"], source_value_text="100")
        )


# ---------------------------------------------------------------- replay


async def test_replay_returns_same_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    first = await svc.create_observation(draft)
    second = await svc.create_observation(draft)
    assert first.replayed is False
    assert second.replayed is True
    assert second.valuation_observation_id == first.valuation_observation_id
    assert second.valuation_observation_fingerprint == first.valuation_observation_fingerprint
    assert await _obs_count(env["sessionmaker"]) == 1


async def test_concurrent_create_single_row(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    results = await asyncio.gather(svc.create_observation(draft), svc.create_observation(draft))
    ids = {r.valuation_observation_id for r in results}
    assert len(ids) == 1
    assert await _obs_count(env["sessionmaker"]) == 1


async def test_value_change_creates_new_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_TWO_VALUES_P))
    card_a = await _card_for_value(env, chunks, "15.3")
    card_b = await _card_for_value(env, chunks, "15.4")
    svc = _service(env)
    a = await svc.create_observation(_obs_draft(env, card_a["evidence_card_id"]))
    b = await svc.create_observation(
        _obs_draft(env, card_b["evidence_card_id"], source_value_text="15.4")
    )
    assert a.valuation_observation_id != b.valuation_observation_id
    assert await _obs_count(env["sessionmaker"]) == 2


async def test_metric_code_change_creates_new_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    svc = _service(env)
    a = await svc.create_observation(_obs_draft(env, card["evidence_card_id"]))
    b = await svc.create_observation(
        _obs_draft(env, card["evidence_card_id"], metric_code=ValuationMetricCode.PS_TTM)
    )
    assert a.valuation_observation_id != b.valuation_observation_id
    assert await _obs_count(env["sessionmaker"]) == 2


async def test_metric_date_change_creates_new_observation(env) -> None:
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    svc = _service(env)
    a = await svc.create_observation(_obs_draft(env, card["evidence_card_id"]))
    b = await svc.create_observation(
        _obs_draft(env, card["evidence_card_id"], metric_as_of=date(2026, 6, 30))
    )
    assert a.valuation_observation_id != b.valuation_observation_id
    assert await _obs_count(env["sessionmaker"]) == 2


async def test_replay_corruption_metric_value_raises_integrity_error(env) -> None:
    """篡改已落库 metric_value → replay 校验发现损坏 → IntegrityError（不 repair）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    first = await svc.create_observation(draft)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE valuation_metric_observations SET metric_value = metric_value + 1 "
                "WHERE valuation_observation_id = :oid"
            ).bindparams(oid=first.valuation_observation_id)
        )
        await session.commit()

    with pytest.raises(ValuationIntegrityError, match="metric_value"):
        await svc.create_observation(draft)


async def test_replay_corruption_source_value_raises_integrity_error(env) -> None:
    """篡改 source_value_text → replay 校验发现损坏 → IntegrityError。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
    svc = _service(env)
    draft = _obs_draft(env, card["evidence_card_id"])
    first = await svc.create_observation(draft)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE valuation_metric_observations SET source_value_text = '篡改' "
                "WHERE valuation_observation_id = :oid"
            ).bindparams(oid=first.valuation_observation_id)
        )
        await session.commit()

    with pytest.raises(ValuationIntegrityError, match="source_value_text"):
        await svc.create_observation(draft)


async def test_evidence_card_unchanged(env) -> None:
    """create_observation 永远不改写 EvidenceCard 行。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
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
    card = await _card_for_value(env, chunks, "15.3")
    result = await _service(env).create_observation(_obs_draft(env, card["evidence_card_id"]))

    async with env["sessionmaker"]() as session:
        obs = (
            await session.execute(
                select(text("source_evidence_card_id, company_id"))
                .select_from(text("valuation_metric_observations"))
                .where(
                    text("valuation_observation_id = :oid").bindparams(
                        oid=result.valuation_observation_id
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
    """create_observation 不创建 Claim / Report（Stage 4C.2A 边界）。"""
    _, _, _, chunks = await _seed_html(env, _fin_html(_SINGLE_VALUE_P))
    card = await _card_for_value(env, chunks, "15.3")
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
                    "('report_sections','reports','review_issues')"
                )
            )
        ).scalar_one()
    assert claim_tables == 2
    assert stage5_tables == 0
    # Stage 5A 的 report_outlines 表已存在（migration 0032），但本阶段不写行。
    outline_rows = (
        await session.execute(text("SELECT count(*) FROM report_outlines"))
    ).scalar_one()
    assert int(outline_rows) == 0


async def test_service_takes_only_sessionmaker(env) -> None:
    """Service 只持有 sessionmaker：无 LLM / LangGraph / Chroma / Report provider。"""
    service = ValuationObservationService(env["sessionmaker"])
    assert set(service.__dict__) == {"_sessionmaker"}
