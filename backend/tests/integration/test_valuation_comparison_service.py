"""RelativeValuationComparisonService integration tests (stage 4C.2A, spec N-S/T/U/V/W/X).

需要真实 PostgreSQL（127.0.0.1:5433）。每个 target / peer 都走真实 SourceRecord →
ParsedSource → ChunkingService → EvidenceCardService（document, metric）→
ValuationObservationService 建 observation，再由 RelativeValuationComparisonService
对比。**零 Chroma / 零 LLM / 零 Claim / 零 Report 表**。

覆盖（spec Z 的 comparison 部分）：
- 创建：显式 peer 集合 → comparison 落库（全部派生字段 / 确定性 stats / 指纹）；
- 确定性统计（spec T/U）：奇数 peer 中位 = 中间值；偶数 peer 中位 = 两中位
  算术平均（ROUND_HALF_EVEN @ 12）；peer_min / peer_max；premium/discount 公式；
- peer 规则（spec O/P/Q）：重复 peer 公司 / peer 含 target 公司 / peer 公司
  与 target observation 公司不一致 → 各自错误；peer 观察缺失 →
  ValuationObservationNotFound；显式 peer，程序不自动选；
- 可比较性（spec R/N）：不同 metric_code / 不同 metric_as_of / 非正 metric_value
  → 各自错误（严格 same-date，不就近交易日对齐）；
- no-lookahead（spec S）：来源 availability 晚于 analysis_as_of →
  ValuationFutureEvidence；analysis_as_of 早于 metric_as_of → ValuationFutureEvidence；
- replay / 并发（spec W/X）：同 fingerprint 复用同一行 / 并发 → 1 comparison +
  1 套完整 peer links；篡改 peer_median → ValuationIntegrityError，不 repair；
- E2E provenance（spec V）：comparison → target Observation → Evidence → Source；
  peer links → peer Observations → Evidences → Sources；
- 边界：不创建 Claim / Report；Service 只持有 sessionmaker。
"""

import asyncio
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import (
    ComparisonDraft,
    ValuationMetricCode,
    ValuationMetricDraft,
)
from app.valuation.errors import (
    ValuationCompanyMismatch,
    ValuationDateMismatch,
    ValuationFutureEvidence,
    ValuationInputError,
    ValuationIntegrityError,
    ValuationMetricMismatch,
    ValuationMetricNotComparable,
    ValuationObservationNotFound,
    ValuationPeerDuplicateError,
    ValuationPeerIncludesTargetError,
)
from app.valuation.observation_service import ValuationObservationService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台当前市盈率水平？"
_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_SOURCE_TITLE = "估值新闻"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_METRIC_AS_OF = date(2026, 8, 7)
_ANALYSIS_AS_OF = date(2026, 8, 10)
_QUANTUM = Decimal("0.000000000001")

# 干净统计样本：target=15.3，peers median=15.0 → premium=+0.02。
_TARGET_VALUE = "15.3"
_PEER_VALUES = ["14.2", "15.0", "16.0"]


def _fin_html(paragraph: str) -> bytes:
    body = f"<p>{paragraph}</p>"
    return (
        "<html><head><title>估值新闻</title></head><body><article>"
        + body
        + "</article></body></html>"
    ).encode()


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def _expected_premium(target: str, median: str) -> Decimal:
    return _quantize((Decimal(target) - Decimal(median)) / Decimal(median))


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
    target_company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "target_company_id": target_company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_company(sessionmaker, code: str) -> UUID:
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code=code,
                identity_key=f"SSE:{code}",
                board="sse_main",
                official_name=f"公司{code}",
                short_name=code,
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    return company_id


async def _seed_observation(
    env: dict,
    company_id: UUID,
    value_text: str,
    *,
    metric_code: ValuationMetricCode = ValuationMetricCode.PE_TTM,
    metric_as_of: date = _METRIC_AS_OF,
    published_at: datetime | None = _PUBLISHED_AT,
) -> dict:
    """真实 HTML metric Evidence → observation（published_at 可覆盖以测 no-lookahead）。"""
    html = _fin_html(f"2026年公司市盈率{value_text}倍，估值水平合理")
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
            company_id=company_id,
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title=_SOURCE_TITLE,
            published_at=published_at,
            reporting_period_end=None,
            source_url=_URL + f"?uid={uuid4().hex[:8]}",
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
    chunk = next(c for c in chunks if value_text in c.text)
    idx = chunk.text.index(value_text)
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="市盈率为" + chunk.text[idx : idx + len(value_text)] + "倍",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + len(value_text),
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await ValuationObservationService(env["sessionmaker"]).create_observation(
        ValuationMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=metric_code,
            metric_as_of=metric_as_of,
            source_value_text=value_text,
        )
    )
    return {
        "company_id": company_id,
        "valuation_observation_id": obs.valuation_observation_id,
        "valuation_observation_fingerprint": obs.valuation_observation_fingerprint,
        "evidence_card_id": card.evidence_card_id,
    }


async def _seed_comparison_set(env: dict) -> dict:
    """seed target + 3 peers（pe_ttm，同一 metric_as_of），返回全部 observation refs。"""
    target = await _seed_observation(env, env["target_company_id"], _TARGET_VALUE)
    peers = []
    for i, value in enumerate(_PEER_VALUES):
        company_id = await _seed_company(env["sessionmaker"], f"6005{2 + i:02d}")
        peers.append(await _seed_observation(env, company_id, value))
    return {"target": target, "peers": peers}


def _draft(env: dict, refs: dict, *, analysis_as_of: date = _ANALYSIS_AS_OF) -> ComparisonDraft:
    return ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=tuple(p["valuation_observation_id"] for p in refs["peers"]),
        analysis_as_of=analysis_as_of,
    )


def _service(env: dict) -> RelativeValuationComparisonService:
    return RelativeValuationComparisonService(env["sessionmaker"])


async def _comparison_row(sessionmaker, comparison_id: UUID):
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT comparison_id, target_company_id, target_observation_id, "
                    "metric_code, metric_as_of, analysis_as_of, comparison_method, "
                    "peer_count, peer_median, peer_min, peer_max, "
                    "premium_discount_to_median, comparison_schema_version, "
                    "formula_version, comparison_fingerprint "
                    "FROM relative_valuation_comparisons WHERE comparison_id = :cid"
                ).bindparams(cid=comparison_id)
            )
        ).one()
        return row


async def _peer_links(sessionmaker, comparison_id: UUID) -> list:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT peer_company_id, peer_observation_id "
                    "FROM relative_valuation_comparison_peers "
                    "WHERE comparison_id = :cid ORDER BY peer_company_id"
                ).bindparams(cid=comparison_id)
            )
        ).all()
        return list(rows)


async def _comparison_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM relative_valuation_comparisons"))
            ).scalar_one()
        )


# ---------------------------------------------------------------- 创建 / 确定性统计


async def test_create_comparison_persists(env) -> None:
    """显式 3 peers → comparison 全字段落库，确定性 stats 正确。"""
    refs = await _seed_comparison_set(env)
    result = await _service(env).create_comparison(_draft(env, refs))
    assert result.replayed is False
    assert len(result.comparison_fingerprint) == 64

    row = await _comparison_row(env["sessionmaker"], result.comparison_id)
    assert row.target_company_id == refs["target"]["company_id"]
    assert row.target_observation_id == refs["target"]["valuation_observation_id"]
    assert row.metric_code == "pe_ttm"
    assert row.metric_as_of == _METRIC_AS_OF
    assert row.analysis_as_of == _ANALYSIS_AS_OF
    assert row.comparison_method == "peer_median"
    assert row.peer_count == 3
    # 奇数 peer 中位 = 中间值；min / max；premium = (15.3-15.0)/15.0 = +0.02。
    assert row.peer_median == Decimal("15.0")
    assert row.peer_min == Decimal("14.2")
    assert row.peer_max == Decimal("16.0")
    assert row.premium_discount_to_median == Decimal("0.02")
    assert row.comparison_schema_version == 1
    assert row.formula_version == 1
    assert row.comparison_fingerprint == result.comparison_fingerprint


async def test_even_peer_count_median_is_arithmetic_mean(env) -> None:
    """偶数 peers [14.2, 15.0, 16.0, 17.0] → 中位 = (15.0+16.0)/2 = 15.5（Decimal）。"""
    target = await _seed_observation(env, env["target_company_id"], "15.3")
    peers = []
    for i, value in enumerate(["14.2", "15.0", "16.0", "17.0"]):
        company_id = await _seed_company(env["sessionmaker"], f"6005{2 + i:02d}")
        peers.append(await _seed_observation(env, company_id, value))
    refs = {"target": target, "peers": peers}
    result = await _service(env).create_comparison(_draft(env, refs))

    row = await _comparison_row(env["sessionmaker"], result.comparison_id)
    assert row.peer_count == 4
    assert row.peer_median == Decimal("15.5")
    assert row.peer_min == Decimal("14.2")
    assert row.peer_max == Decimal("17.0")
    assert row.premium_discount_to_median == _expected_premium("15.3", "15.5")


async def test_premium_discount_negative_quantized(env) -> None:
    """premium 除法含无限循环小数 → ROUND_HALF_EVEN @ 12 位确定性舍入（无 float）。"""
    target = await _seed_observation(env, env["target_company_id"], "15.3")
    peers = []
    for i, value in enumerate(["14.2", "15.0", "16.0", "17.0"]):
        company_id = await _seed_company(env["sessionmaker"], f"6005{2 + i:02d}")
        peers.append(await _seed_observation(env, company_id, value))
    refs = {"target": target, "peers": peers}
    result = await _service(env).create_comparison(_draft(env, refs))

    row = await _comparison_row(env["sessionmaker"], result.comparison_id)
    assert row.peer_median == Decimal("15.5")
    # -0.2/15.5 = -0.0129032258064516...（无限循环）→ 12 位 = -0.012903225806
    # （13 位是 4，ROUND_HALF_EVEN 向下舍）。Decimal 全精度除法，无 float 误差。
    assert row.premium_discount_to_median == Decimal("-0.012903225806")
    assert row.premium_discount_to_median == _expected_premium("15.3", "15.5")


# ---------------------------------------------------------------- replay / 并发


async def test_replay_returns_same_comparison(env) -> None:
    refs = await _seed_comparison_set(env)
    svc = _service(env)
    draft = _draft(env, refs)
    first = await svc.create_comparison(draft)
    second = await svc.create_comparison(draft)
    assert first.replayed is False
    assert second.replayed is True
    assert second.comparison_id == first.comparison_id
    assert second.comparison_fingerprint == first.comparison_fingerprint
    assert await _comparison_count(env["sessionmaker"]) == 1
    assert len(await _peer_links(env["sessionmaker"], first.comparison_id)) == 3


async def test_concurrent_create_single_comparison_complete_peers(env) -> None:
    """并发相同 draft → 1 comparison + 1 套完整 peer links（无 partial write）。"""
    refs = await _seed_comparison_set(env)
    svc = _service(env)
    draft = _draft(env, refs)
    results = await asyncio.gather(svc.create_comparison(draft), svc.create_comparison(draft))
    ids = {r.comparison_id for r in results}
    assert len(ids) == 1
    assert await _comparison_count(env["sessionmaker"]) == 1
    assert len(await _peer_links(env["sessionmaker"], ids.pop())) == 3


async def test_peer_links_exact_set(env) -> None:
    """peer links = 显式 peer 集合（company, observation 一一对应，不复制 evidence id）。"""
    refs = await _seed_comparison_set(env)
    result = await _service(env).create_comparison(_draft(env, refs))
    links = await _peer_links(env["sessionmaker"], result.comparison_id)
    expected = {(p["company_id"], p["valuation_observation_id"]) for p in refs["peers"]}
    actual = {(link.peer_company_id, link.peer_observation_id) for link in links}
    assert actual == expected
    assert len(links) == 3


# ---------------------------------------------------------------- 拒绝：输入 / peer 规则


async def test_peer_count_below_minimum_rejected(env) -> None:
    refs = await _seed_comparison_set(env)
    with pytest.raises(ValuationInputError):
        ComparisonDraft(
            target_company_id=refs["target"]["company_id"],
            target_observation_id=refs["target"]["valuation_observation_id"],
            peer_observation_ids=tuple(p["valuation_observation_id"] for p in refs["peers"][:2]),
            analysis_as_of=_ANALYSIS_AS_OF,
        )


async def test_duplicate_peer_observation_id_rejected(env) -> None:
    refs = await _seed_comparison_set(env)
    peer_ids = [p["valuation_observation_id"] for p in refs["peers"]]
    with pytest.raises(ValuationInputError):
        ComparisonDraft(
            target_company_id=refs["target"]["company_id"],
            target_observation_id=refs["target"]["valuation_observation_id"],
            peer_observation_ids=(peer_ids[0], peer_ids[0], peer_ids[1]),
            analysis_as_of=_ANALYSIS_AS_OF,
        )


async def test_missing_peer_observation_rejected(env) -> None:
    refs = await _seed_comparison_set(env)
    refs = {
        "target": refs["target"],
        "peers": [refs["peers"][0], refs["peers"][1], {"valuation_observation_id": uuid4()}],
    }
    with pytest.raises(ValuationObservationNotFound):
        await _service(env).create_comparison(_draft(env, refs))


async def test_target_company_mismatch_rejected(env) -> None:
    refs = await _seed_comparison_set(env)
    other = await _seed_company(env["sessionmaker"], "600599")
    draft = _draft(env, refs)
    bad = ComparisonDraft(
        target_company_id=other,
        target_observation_id=draft.target_observation_id,
        peer_observation_ids=draft.peer_observation_ids,
        analysis_as_of=draft.analysis_as_of,
    )
    with pytest.raises(ValuationCompanyMismatch):
        await _service(env).create_comparison(bad)


async def test_duplicate_peer_company_rejected(env) -> None:
    """两个 peer observation 属于同一公司 → ValuationPeerDuplicateError。"""
    refs = await _seed_comparison_set(env)
    company = await _seed_company(env["sessionmaker"], "600598")
    extra = await _seed_observation(env, company, "13.0")
    extra2 = await _seed_observation(env, company, "13.5")
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=(
            extra["valuation_observation_id"],
            extra2["valuation_observation_id"],
            refs["peers"][0]["valuation_observation_id"],
        ),
        analysis_as_of=_ANALYSIS_AS_OF,
    )
    with pytest.raises(ValuationPeerDuplicateError):
        await _service(env).create_comparison(draft)


async def test_peer_includes_target_company_rejected(env) -> None:
    """peer 集合含 target 公司 observation → ValuationPeerIncludesTargetError。"""
    refs = await _seed_comparison_set(env)
    target_peer = await _seed_observation(env, env["target_company_id"], "12.5")
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=(
            refs["peers"][0]["valuation_observation_id"],
            refs["peers"][1]["valuation_observation_id"],
            target_peer["valuation_observation_id"],
        ),
        analysis_as_of=_ANALYSIS_AS_OF,
    )
    with pytest.raises(ValuationPeerIncludesTargetError):
        await _service(env).create_comparison(draft)


# ---------------------------------------------------------------- 可比较性 / no-lookahead


async def test_metric_mismatch_rejected(env) -> None:
    """比较集合内存在不同 metric_code → ValuationMetricMismatch。"""
    refs = await _seed_comparison_set(env)
    company = await _seed_company(env["sessionmaker"], "600597")
    pb = await _seed_observation(env, company, "2.5", metric_code=ValuationMetricCode.PB_MRQ)
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=(
            refs["peers"][0]["valuation_observation_id"],
            refs["peers"][1]["valuation_observation_id"],
            pb["valuation_observation_id"],
        ),
        analysis_as_of=_ANALYSIS_AS_OF,
    )
    with pytest.raises(ValuationMetricMismatch):
        await _service(env).create_comparison(draft)


async def test_date_mismatch_rejected(env) -> None:
    """比较集合内存在不同 metric_as_of → ValuationDateMismatch（严格 same-date）。"""
    refs = await _seed_comparison_set(env)
    company = await _seed_company(env["sessionmaker"], "600596")
    stale = await _seed_observation(env, company, "14.0", metric_as_of=date(2026, 6, 30))
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=(
            refs["peers"][0]["valuation_observation_id"],
            refs["peers"][1]["valuation_observation_id"],
            stale["valuation_observation_id"],
        ),
        analysis_as_of=_ANALYSIS_AS_OF,
    )
    with pytest.raises(ValuationDateMismatch):
        await _service(env).create_comparison(draft)


async def test_non_positive_peer_rejected(env) -> None:
    """0 倍数可作为 observation 快照，但 comparison 拒绝（ValuationMetricNotComparable）。"""
    refs = await _seed_comparison_set(env)
    company = await _seed_company(env["sessionmaker"], "600595")
    zero = await _seed_observation(env, company, "0")
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=(
            refs["peers"][0]["valuation_observation_id"],
            refs["peers"][1]["valuation_observation_id"],
            zero["valuation_observation_id"],
        ),
        analysis_as_of=_ANALYSIS_AS_OF,
    )
    with pytest.raises(ValuationMetricNotComparable):
        await _service(env).create_comparison(draft)


async def test_future_evidence_rejected(env) -> None:
    """peer 来源 published_at 晚于 analysis_as_of → ValuationFutureEvidence。"""
    refs = await _seed_comparison_set(env)
    company = await _seed_company(env["sessionmaker"], "600594")
    future = await _seed_observation(
        env,
        company,
        "14.0",
        published_at=datetime(2026, 8, 11, 9, 30, tzinfo=UTC),  # 晚于 analysis 2026-08-10
    )
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=(
            refs["peers"][0]["valuation_observation_id"],
            refs["peers"][1]["valuation_observation_id"],
            future["valuation_observation_id"],
        ),
        analysis_as_of=_ANALYSIS_AS_OF,
    )
    with pytest.raises(ValuationFutureEvidence):
        await _service(env).create_comparison(draft)


async def test_analysis_before_metric_date_rejected(env) -> None:
    """analysis_as_of 早于 metric_as_of → ValuationFutureEvidence（no-lookahead）。"""
    refs = await _seed_comparison_set(env)
    draft = ComparisonDraft(
        target_company_id=refs["target"]["company_id"],
        target_observation_id=refs["target"]["valuation_observation_id"],
        peer_observation_ids=tuple(p["valuation_observation_id"] for p in refs["peers"]),
        analysis_as_of=date(2026, 8, 6),  # < metric_as_of 2026-08-07
    )
    with pytest.raises(ValuationFutureEvidence, match="no-lookahead"):
        await _service(env).create_comparison(draft)


# ---------------------------------------------------------------- integrity


async def test_corrupt_stats_raises_integrity_error(env) -> None:
    """篡改已落库 peer_median → replay 校验发现损坏 → IntegrityError（不 repair）。"""
    refs = await _seed_comparison_set(env)
    svc = _service(env)
    draft = _draft(env, refs)
    first = await svc.create_comparison(draft)

    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE relative_valuation_comparisons SET peer_median = peer_median + 1 "
                "WHERE comparison_id = :cid"
            ).bindparams(cid=first.comparison_id)
        )
        await session.commit()

    with pytest.raises(ValuationIntegrityError, match="peer_median"):
        await svc.create_comparison(draft)


async def test_no_partial_write_on_integrity_failure(env) -> None:
    """篡改后 recreate 抛 IntegrityError，比较集合行数与 peer links 均不新增。"""
    refs = await _seed_comparison_set(env)
    svc = _service(env)
    draft = _draft(env, refs)
    first = await svc.create_comparison(draft)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE relative_valuation_comparisons SET peer_min = peer_min + 1 "
                "WHERE comparison_id = :cid"
            ).bindparams(cid=first.comparison_id)
        )
        await session.commit()
    with pytest.raises(ValuationIntegrityError, match="peer_min"):
        await svc.create_comparison(draft)
    assert await _comparison_count(env["sessionmaker"]) == 1
    assert len(await _peer_links(env["sessionmaker"], first.comparison_id)) == 3


# ------------------------------------------------- Gate 0：verify_comparison_integrity 稳定错误边界


async def test_verify_comparison_integrity_missing_returns_none(env) -> None:
    """comparison_id 不存在 → verify_comparison_integrity 返回 None（非普通输入错误）。"""
    async with env["sessionmaker"]() as session:
        verified = await _service(env).verify_comparison_integrity(session, uuid4())
    assert verified is None


async def test_verify_comparison_integrity_returns_verified(env) -> None:
    """完好 comparison → 返回 VerifiedComparison（真实 target/peer Observation + Evidence）。"""
    refs = await _seed_comparison_set(env)
    result = await _service(env).create_comparison(_draft(env, refs))
    async with env["sessionmaker"]() as session:
        verified = await _service(env).verify_comparison_integrity(session, result.comparison_id)
    assert verified is not None
    assert verified.comparison_id == result.comparison_id
    assert verified.comparison_fingerprint == result.comparison_fingerprint
    assert verified.target_company_id == refs["target"]["company_id"]
    assert verified.metric_code == "pe_ttm"
    assert verified.peer_companies == tuple(
        sorted((p["company_id"] for p in refs["peers"]), key=str)
    )
    assert len(verified.peer_observations) == 3
    # evidence 覆盖 target + 全部 peers 的 source EvidenceCard。
    assert len(verified.evidence) == 4


async def test_verify_comparison_integrity_deleted_peer_link_raises_integrity(env) -> None:
    """删除一条 persisted peer link（<3）→ verify 抛 ValuationIntegrityError（包装自
    ValuationInputError），不泄漏普通 input error。"""
    refs = await _seed_comparison_set(env)
    result = await _service(env).create_comparison(_draft(env, refs))
    async with env["sessionmaker"]() as session:
        # 精确删除 peer[0] 的 link，剩 2 条（<3）。
        await session.execute(
            text(
                "DELETE FROM relative_valuation_comparison_peers "
                "WHERE comparison_id = :cid AND peer_company_id = :pid"
            ).bindparams(cid=result.comparison_id, pid=refs["peers"][0]["company_id"])
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        with pytest.raises(ValuationIntegrityError) as excinfo:
            await _service(env).verify_comparison_integrity(session, result.comparison_id)
    # 必须保留 cause 链（raise ... from exc）。
    assert isinstance(excinfo.value.__cause__, ValuationInputError)


async def test_verify_comparison_integrity_target_in_peer_links_raises_integrity(env) -> None:
    """把一条 persisted peer link 指向 target observation → verify 抛
    ValuationIntegrityError（包装自 ValuationInputError）。"""
    refs = await _seed_comparison_set(env)
    result = await _service(env).create_comparison(_draft(env, refs))
    async with env["sessionmaker"]() as session:
        # 精确把 peer[0] 的 link 指向 target observation（含 target 进 peer 集合）。
        await session.execute(
            text(
                "UPDATE relative_valuation_comparison_peers SET peer_observation_id = :toid "
                "WHERE comparison_id = :cid AND peer_company_id = :pid"
            ).bindparams(
                toid=refs["target"]["valuation_observation_id"],
                cid=result.comparison_id,
                pid=refs["peers"][0]["company_id"],
            )
        )
        await session.commit()
    async with env["sessionmaker"]() as session:
        with pytest.raises(ValuationIntegrityError) as excinfo:
            await _service(env).verify_comparison_integrity(session, result.comparison_id)
    assert isinstance(excinfo.value.__cause__, ValuationInputError)


# ---------------------------------------------------------------- E2E provenance


async def test_comparison_provenance_full_chain(env) -> None:
    """comparison → target/peer Observation → Evidence → Source（公司一致）。"""
    refs = await _seed_comparison_set(env)
    result = await _service(env).create_comparison(_draft(env, refs))

    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                text(
                    "SELECT target_company_id, target_observation_id, metric_code "
                    "FROM relative_valuation_comparisons WHERE comparison_id = :cid"
                ).bindparams(cid=result.comparison_id)
            )
        ).one()
        # target 链：observation → evidence → source。
        target_evidence = (
            await session.execute(
                text(
                    "SELECT e.source_id, e.company_id FROM evidence_cards e "
                    "JOIN valuation_metric_observations o "
                    "ON o.source_evidence_card_id = e.evidence_card_id "
                    "WHERE o.valuation_observation_id = :oid"
                ).bindparams(oid=row.target_observation_id)
            )
        ).one()
        target_source = (
            await session.execute(
                text(
                    "SELECT company_id, published_at, acquired_at FROM source_records "
                    "WHERE source_id = :sid"
                ).bindparams(sid=target_evidence.source_id)
            )
        ).one()
        assert target_evidence.company_id == row.target_company_id
        assert target_source.company_id == row.target_company_id
        assert target_source.published_at is not None  # document 卡 availability 用 published_at
        assert target_source.published_at.date() <= _ANALYSIS_AS_OF
        # 所有 peer 链：peer link → observation → evidence → source。
        links = (
            await session.execute(
                text(
                    "SELECT peer_company_id, peer_observation_id "
                    "FROM relative_valuation_comparison_peers "
                    "WHERE comparison_id = :cid"
                ).bindparams(cid=result.comparison_id)
            )
        ).all()
        assert len(links) == 3
        for link in links:
            peer_evidence = (
                await session.execute(
                    text(
                        "SELECT e.source_id, e.company_id FROM evidence_cards e "
                        "JOIN valuation_metric_observations o "
                        "ON o.source_evidence_card_id = e.evidence_card_id "
                        "WHERE o.valuation_observation_id = :oid"
                    ).bindparams(oid=link.peer_observation_id)
                )
            ).one()
            peer_source = (
                await session.execute(
                    text("SELECT company_id FROM source_records WHERE source_id = :sid").bindparams(
                        sid=peer_evidence.source_id
                    )
                )
            ).one()
            assert peer_evidence.company_id == link.peer_company_id
            assert peer_source.company_id == link.peer_company_id


# ---------------------------------------------------------------- 边界


async def test_no_claims_or_report_tables(env) -> None:
    """create_comparison 不创建 Claim / Report（Stage 4C.2A 边界）。"""
    refs = await _seed_comparison_set(env)
    await _service(env).create_comparison(_draft(env, refs))
    async with env["sessionmaker"]() as session:
        stage5_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('report_sections','reports','review_issues')"
                )
            )
        ).scalar_one()
    assert stage5_tables == 0
    # Stage 5A 的 report_outlines 表已存在（migration 0032），但本阶段不写行。
    outline_rows = (
        await session.execute(text("SELECT count(*) FROM report_outlines"))
    ).scalar_one()
    assert int(outline_rows) == 0


async def test_service_takes_only_sessionmaker(env) -> None:
    """Service 只持有 sessionmaker：无 LLM / LangGraph / Chroma / Report provider。"""
    service = RelativeValuationComparisonService(env["sessionmaker"])
    assert set(service.__dict__) == {"_sessionmaker"}
