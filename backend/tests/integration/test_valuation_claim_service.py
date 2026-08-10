"""ValuationClaimService integration tests (stage 4C.2B.1, spec W).

需要真实 PostgreSQL（127.0.0.1:5433）。公司 / Observations / Comparisons 用
真实服务链 seed（`_seed_observation` + RelativeValuationComparisonService）；
claim 经 ValuationClaimService.create_claim / create_claim_batch 创建。**零
Chroma / 零 LLM / 零 LangGraph / 零 Report / 零 Audit**。

覆盖（spec W）：
- Gate：0027 两张表存在；无 Stage 5 report 表；service 只持有 sessionmaker；
- 创建：Claim（claim_schema_version=7，analysis_domain=valuation，
  claim_kind=relative_valuation）+ RelativeValuationClaimProfile +
  ClaimRelativeValuationComparisonLink + ClaimEvidenceLink（automatic source
  Evidence expansion，一律 relation=context）原子落库；
- 拒绝：Comparison 缺失 / Comparison 跨公司 / analysis_as_of 不一致 /
  metric_as_of 不一致 / metric_code 重复 / peer 集合不一致 / comparison
  损坏（replay 校验失败）/ additional Evidence 缺失或跨公司 / automatic 与
  additional relation 冲突 / critical 缺全部 eligible source Evidence；
- automatic expansion：每 comparison → target + 全部 peer Observations 的
  source EvidenceCards 自动 context；跨 comparison / additional context
  幂等去重；
- critical policy：每个 support Comparison 的 target + peer 全部 source
  Evidence 必须 eligible；additional supports 不能替代；
- replay / 并发：同 fingerprint 复用同一行 / 并发 → 1 完整集合 / comparison
  变化 → 新 Claim；损坏（profile / comparison link / evidence link /
  comparison）→ ValuationClaimIntegrityError，**不自动 repair**；
- batch：ordered result / mixed replay+create / later failure full rollback /
  out-of-range；
- E2E provenance：Claim → ClaimRelativeValuationComparisonLink →
  RelativeValuationComparison → ValuationMetricObservation → EvidenceCard →
  Source（target + 全部 peers）。
"""

import asyncio
from datetime import UTC, date, datetime
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
from app.repositories.claim_repository import ClaimRepository
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.claim_contracts import (
    MAX_VALUATION_CLAIMS_PER_BATCH,
    VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
    VALUATION_CLAIM_SCHEMA_VERSION,
    ValuationClaimAssessment,
    ValuationClaimBatchResult,
    ValuationClaimConfidence,
    ValuationClaimDraft,
    ValuationClaimImportance,
)
from app.valuation.claim_errors import (
    ValuationClaimAnalysisDateMismatch,
    ValuationClaimComparisonMismatch,
    ValuationClaimComparisonNotFound,
    ValuationClaimCriticalEvidenceInsufficient,
    ValuationClaimDraftError,
    ValuationClaimDuplicateMetric,
    ValuationClaimEvidenceCompanyMismatch,
    ValuationClaimIntegrityError,
    ValuationClaimMetricDateMismatch,
    ValuationClaimPeerSetMismatch,
    ValuationClaimRelationConflict,
)
from app.valuation.claim_service import ValuationClaimService
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft, ValuationMetricCode, ValuationMetricDraft
from app.valuation.observation_service import ValuationObservationService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台当前市盈率水平？"
_STATEMENT = "贵州茅台当前估值水平与可比公司基本一致。"
_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_SOURCE_TITLE = "估值新闻"
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_METRIC_AS_OF = date(2026, 8, 7)
_ANALYSIS_AS_OF = date(2026, 8, 10)

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
        await session.execute(text("DELETE FROM claim_relative_valuation_comparison_links"))
        await session.execute(text("DELETE FROM relative_valuation_claim_profiles"))
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
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
    peer_company_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "target_company_id": target_company_id,
        "peer_company_ids": peer_company_ids,
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
    critical_claim_eligible: bool = False,
) -> dict:
    """真实 HTML metric Evidence → observation（source 可 eligible 供 critical 测）。"""
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
            published_at=_PUBLISHED_AT,
            reporting_period_end=None,
            source_url=_URL + f"?uid={uuid4().hex[:8]}",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=critical_claim_eligible,
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
        "evidence_card_id": card.evidence_card_id,
    }


async def _seed_comparison(
    env: dict,
    *,
    metric_code: ValuationMetricCode = ValuationMetricCode.PE_TTM,
    metric_as_of: date = _METRIC_AS_OF,
    analysis_as_of: date = _ANALYSIS_AS_OF,
    peer_company_ids: list[UUID] | None = None,
    target_company_id: UUID | None = None,
    critical_claim_eligible: bool = False,
):
    """target + 3 peers（同 metric / 同 metric_as_of）→ 真实 comparison。返回 ComparisonResult。"""
    target_company = (
        target_company_id if target_company_id is not None else env["target_company_id"]
    )
    peers = peer_company_ids if peer_company_ids is not None else env["peer_company_ids"]
    target = await _seed_observation(
        env,
        target_company,
        _TARGET_VALUE,
        metric_code=metric_code,
        metric_as_of=metric_as_of,
        critical_claim_eligible=critical_claim_eligible,
    )
    peer_obs = []
    for i, value in enumerate(_PEER_VALUES):
        peer_obs.append(
            await _seed_observation(
                env,
                peers[i],
                value,
                metric_code=metric_code,
                metric_as_of=metric_as_of,
                critical_claim_eligible=critical_claim_eligible,
            )
        )
    return await RelativeValuationComparisonService(env["sessionmaker"]).create_comparison(
        ComparisonDraft(
            target_company_id=target_company,
            target_observation_id=target["valuation_observation_id"],
            peer_observation_ids=tuple(p["valuation_observation_id"] for p in peer_obs),
            analysis_as_of=analysis_as_of,
        )
    )


def _claim_draft(
    env: dict,
    *,
    supports,
    contradicts=(),
    context=(),
    add_supports=(),
    add_contradicts=(),
    add_context=(),
    assessment=ValuationClaimAssessment.BROADLY_IN_LINE,
    importance=ValuationClaimImportance.NORMAL,
    analysis_as_of: date = _ANALYSIS_AS_OF,
    **overrides,
) -> ValuationClaimDraft:
    values = dict(
        company_id=env["target_company_id"],
        research_question=_QUESTION,
        analysis_as_of=analysis_as_of,
        statement=_STATEMENT,
        assessment=assessment,
        confidence=ValuationClaimConfidence.HIGH,
        importance=importance,
        support_comparison_ids=list(supports),
        contradict_comparison_ids=list(contradicts),
        context_comparison_ids=list(context),
        additional_support_evidence_ids=list(add_supports),
        additional_contradict_evidence_ids=list(add_contradicts),
        additional_context_evidence_ids=list(add_context),
        analyst_name="structured-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    values.update(overrides)
    return ValuationClaimDraft(**values)


def _service(env: dict) -> ValuationClaimService:
    return ValuationClaimService(env["sessionmaker"])


async def _claim_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM claims WHERE claim_schema_version = 7")
                )
            ).scalar_one()
        )


async def _profile_rows(sessionmaker, claim_id: UUID) -> tuple[str, date, int]:
    async with sessionmaker() as session:
        row = (
            await session.execute(
                text(
                    "SELECT assessment, analysis_as_of, profile_schema_version "
                    "FROM relative_valuation_claim_profiles WHERE claim_id = :cid"
                ).bindparams(cid=claim_id)
            )
        ).one()
        return str(row[0]), row[1], int(row[2])


async def _comp_link_rows(sessionmaker, claim_id: UUID) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT comparison_id, relation "
                    "FROM claim_relative_valuation_comparison_links "
                    "WHERE claim_id = :cid"
                ).bindparams(cid=claim_id)
            )
        ).all()
        return sorted((str(r[0]), str(r[1])) for r in rows)


async def _evidence_link_rows(sessionmaker, claim_id: UUID) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT evidence_card_id, relation FROM claim_evidence_links "
                    "WHERE claim_id = :cid"
                ).bindparams(cid=claim_id)
            )
        ).all()
        return sorted((str(r[0]), str(r[1])) for r in rows)


# ---------------------------------------------------------------- Gate / 创建


async def test_gate_0027_tables_exist_no_stage5(env) -> None:
    async with env["sessionmaker"]() as session:
        tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('claim_relative_valuation_comparison_links',"
                    "'relative_valuation_claim_profiles')"
                )
            )
        ).scalar_one()
        assert tables == 2
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
    service = ValuationClaimService(env["sessionmaker"])
    assert set(service.__dict__) == {"_sessionmaker"}


async def test_create_claim_persists_claim_profile_comp_links(env) -> None:
    comp = await _seed_comparison(env)
    result = await _service(env).create_claim(_claim_draft(env, supports=[comp.comparison_id]))

    assert result.replayed is False
    assert len(result.claim_fingerprint) == 64
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.company_id == env["target_company_id"]
    assert claim.analysis_domain == "valuation"
    assert claim.claim_kind == "relative_valuation"
    assert claim.claim_schema_version == VALUATION_CLAIM_SCHEMA_VERSION  # 7
    assert claim.claim_fingerprint == result.claim_fingerprint
    # Profile：assessment / analysis_as_of / profile_schema_version 落库。
    assert await _profile_rows(env["sessionmaker"], result.claim_id) == (
        "broadly_in_line",
        _ANALYSIS_AS_OF,
        VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
    )
    # Comparison link：supports 落库。
    assert await _comp_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(comp.comparison_id), "supports")
    ]
    # Automatic Evidence expansion：target + 全部 peer Observations 的 source
    # EvidenceCards 自动进入 context links（4 张卡）。
    links = await _evidence_link_rows(env["sessionmaker"], result.claim_id)
    assert len(links) == 4
    assert all(relation == "context" for _, relation in links)
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_create_claim_automatic_evidence_context_links(env) -> None:
    """自动展开：comparison 的 target + 3 peers 的 source EvidenceCards 全部
    relation=context（4 张），不产生 supports/contradicts。"""
    comp = await _seed_comparison(env)
    result = await _service(env).create_claim(_claim_draft(env, supports=[comp.comparison_id]))
    links = await _evidence_link_rows(env["sessionmaker"], result.claim_id)
    assert len(links) == 4
    assert all(relation == "context" for _, relation in links)


async def test_create_claim_additional_evidence_merged(env) -> None:
    """additional Evidence 保持 caller 指定 relation（supports），不进 comparison links。"""
    comp = await _seed_comparison(env)
    # 额外一张 target 公司卡（不参与任何 comparison）。
    extra = await _seed_observation(env, env["target_company_id"], "12.8")
    result = await _service(env).create_claim(
        _claim_draft(env, supports=[comp.comparison_id], add_supports=[extra["evidence_card_id"]])
    )
    links = await _evidence_link_rows(env["sessionmaker"], result.claim_id)
    assert (str(extra["evidence_card_id"]), "supports") in links
    assert sum(1 for _, relation in links if relation == "supports") == 1
    # additional 不进 comparison links。
    assert await _comp_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(comp.comparison_id), "supports")
    ]


async def test_additional_context_same_as_automatic_dedupes(env) -> None:
    """additional context 指向自动展开的 source Evidence（同 relation=context）→
    幂等去重，不冲突、不重复。"""
    comp = await _seed_comparison(env)
    async with env["sessionmaker"]() as session:
        card_id = (
            await session.execute(
                text(
                    "SELECT ec.evidence_card_id FROM valuation_metric_observations o "
                    "JOIN evidence_cards ec ON ec.evidence_card_id = o.source_evidence_card_id "
                    "WHERE o.valuation_observation_id = "
                    "(SELECT target_observation_id FROM relative_valuation_comparisons "
                    " WHERE comparison_id = :cid)"
                ).bindparams(cid=comp.comparison_id)
            )
        ).scalar_one()
    result = await _service(env).create_claim(
        _claim_draft(env, supports=[comp.comparison_id], add_context=[card_id])
    )
    links = await _evidence_link_rows(env["sessionmaker"], result.claim_id)
    assert (str(card_id), "context") in links
    assert len(links) == 4  # 仍是 4 张（去重后），不重复
    assert all(relation == "context" for _, relation in links)


# ---------------------------------------------------------------- 拒绝


async def test_comparison_missing_rejected(env) -> None:
    ghost = uuid4()
    with pytest.raises(ValuationClaimComparisonNotFound):
        await _service(env).create_claim(_claim_draft(env, supports=[ghost]))
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_comparison_company_mismatch_rejected(env) -> None:
    comp = await _seed_comparison(env)
    other = await _seed_company(env["sessionmaker"], "600599")
    draft = _claim_draft(env, supports=[comp.comparison_id], company_id=other)
    with pytest.raises(ValuationClaimComparisonMismatch):
        await _service(env).create_claim(draft)
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analysis_date_mismatch_rejected(env) -> None:
    comp = await _seed_comparison(env, analysis_as_of=_ANALYSIS_AS_OF)
    draft = _claim_draft(env, supports=[comp.comparison_id], analysis_as_of=date(2026, 8, 9))
    with pytest.raises(ValuationClaimAnalysisDateMismatch):
        await _service(env).create_claim(draft)
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_metric_date_mismatch_rejected(env) -> None:
    """两个 comparison 使用不同 metric_as_of → ValuationClaimMetricDateMismatch。"""
    comp_a = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    comp_b = await _seed_comparison(
        env,
        metric_code=ValuationMetricCode.PB_MRQ,
        metric_as_of=date(2026, 7, 31),
    )
    with pytest.raises(ValuationClaimMetricDateMismatch):
        await _service(env).create_claim(
            _claim_draft(env, supports=[comp_a.comparison_id, comp_b.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_duplicate_metric_rejected(env) -> None:
    """一个 claim 内 metric_code 重复（两个 PE comparison，peer 集合一致）
    → ValuationClaimDuplicateMetric（peer-set 一致，只留下 metric 重复）。"""
    comp_a = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    comp_b = await _seed_comparison(
        env,
        metric_code=ValuationMetricCode.PE_TTM,
        peer_company_ids=env["peer_company_ids"],
    )
    with pytest.raises(ValuationClaimDuplicateMetric):
        await _service(env).create_claim(
            _claim_draft(env, supports=[comp_a.comparison_id, comp_b.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_peer_set_mismatch_rejected(env) -> None:
    """两个 comparison 的 peer_company_id 集合不一致 → ValuationClaimPeerSetMismatch。"""
    comp_a = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    different_peers = [await _seed_company(env["sessionmaker"], "600588")] + env[
        "peer_company_ids"
    ][1:]
    comp_b = await _seed_comparison(
        env,
        metric_code=ValuationMetricCode.PB_MRQ,
        peer_company_ids=different_peers,
    )
    with pytest.raises(ValuationClaimPeerSetMismatch):
        await _service(env).create_claim(
            _claim_draft(env, supports=[comp_a.comparison_id, comp_b.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_comparison_corruption_rejected_no_repair(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    await _service(env).create_claim(draft)

    # 篡改上游 comparison 的 peer_median → 重新 create 时 comparison replay 失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE relative_valuation_comparisons SET peer_median = peer_median + 1 "
                "WHERE comparison_id = :cid"
            ).bindparams(cid=comp.comparison_id)
        )
        await session.commit()

    with pytest.raises(ValuationClaimIntegrityError):
        await _service(env).create_claim(draft)
    assert await _claim_count(env["sessionmaker"]) == 1  # 既有 claim 保留


async def test_additional_evidence_missing_rejected(env) -> None:
    comp = await _seed_comparison(env)
    ghost = uuid4()
    with pytest.raises(ValuationClaimEvidenceCompanyMismatch):
        await _service(env).create_claim(
            _claim_draft(env, supports=[comp.comparison_id], add_supports=[ghost])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_additional_evidence_cross_company_rejected(env) -> None:
    """peer company 的 Evidence 不能作为 target 的 additional Evidence。"""
    comp = await _seed_comparison(env)
    other = await _seed_company(env["sessionmaker"], "600599")
    peer_card = await _seed_observation(env, other, "13.0")
    with pytest.raises(ValuationClaimEvidenceCompanyMismatch):
        await _service(env).create_claim(
            _claim_draft(
                env, supports=[comp.comparison_id], add_supports=[peer_card["evidence_card_id"]]
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_relation_conflict_rejected(env) -> None:
    """additional_supports 指定自动展开的 source Evidence（context）→ 冲突拒绝。"""
    comp = await _seed_comparison(env)
    async with env["sessionmaker"]() as session:
        card_id = (
            await session.execute(
                text(
                    "SELECT ec.evidence_card_id FROM valuation_metric_observations o "
                    "JOIN evidence_cards ec ON ec.evidence_card_id = o.source_evidence_card_id "
                    "WHERE o.valuation_observation_id = "
                    "(SELECT target_observation_id FROM relative_valuation_comparisons "
                    " WHERE comparison_id = :cid)"
                ).bindparams(cid=comp.comparison_id)
            )
        ).scalar_one()
    with pytest.raises(ValuationClaimRelationConflict):
        await _service(env).create_claim(
            _claim_draft(env, supports=[comp.comparison_id], add_supports=[card_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_draft_requires_support_comparison(env) -> None:
    with pytest.raises(ValuationClaimDraftError, match="support_comparison_id"):
        _claim_draft(env, supports=[], context=[uuid4()])


# ---------------------------------------------------------------- critical policy


async def test_critical_all_source_eligible_accepted(env) -> None:
    """critical：support comparison 的 target + 全部 peer source Evidence 全部
    eligible → accept。"""
    comp = await _seed_comparison(env, critical_claim_eligible=True)
    result = await _service(env).create_claim(
        _claim_draft(
            env,
            supports=[comp.comparison_id],
            importance=ValuationClaimImportance.CRITICAL,
        )
    )
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.importance == "critical"


async def test_critical_without_all_eligible_rejected(env) -> None:
    """critical：任一 support comparison 的任一 source Evidence 不 eligible →
    ValuationClaimCriticalEvidenceInsufficient。"""
    comp = await _seed_comparison(env, critical_claim_eligible=False)
    with pytest.raises(ValuationClaimCriticalEvidenceInsufficient):
        await _service(env).create_claim(
            _claim_draft(
                env,
                supports=[comp.comparison_id],
                importance=ValuationClaimImportance.CRITICAL,
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_critical_additional_support_not_substitute(env) -> None:
    """critical：additional supports 不能替代——support comparison source 不
    eligible + additional_supports 有 eligible 卡 → 仍拒绝。"""
    comp = await _seed_comparison(env, critical_claim_eligible=False)
    eligible = await _seed_observation(
        env, env["target_company_id"], "12.8", critical_claim_eligible=True
    )
    with pytest.raises(ValuationClaimCriticalEvidenceInsufficient):
        await _service(env).create_claim(
            _claim_draft(
                env,
                supports=[comp.comparison_id],
                add_supports=[eligible["evidence_card_id"]],
                importance=ValuationClaimImportance.CRITICAL,
            )
        )
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- replay / 并发 / integrity


async def test_replay_returns_same_claim(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    svc = _service(env)
    first = await svc.create_claim(draft)
    second = await svc.create_claim(draft)
    assert first.replayed is False
    assert second.replayed is True
    assert second.claim_id == first.claim_id
    assert second.claim_fingerprint == first.claim_fingerprint
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_concurrent_create_single_complete_set(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    svc = _service(env)
    results = await asyncio.gather(*(svc.create_claim(draft) for _ in range(5)))
    ids = {r.claim_id for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r.replayed) == 4
    assert await _claim_count(env["sessionmaker"]) == 1
    claim_id = next(iter(ids))
    # 完整集合：1 profile + 1 comparison link + 4 context evidence links。
    assert await _profile_rows(env["sessionmaker"], claim_id) == (
        "broadly_in_line",
        _ANALYSIS_AS_OF,
        VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
    )
    assert await _comp_link_rows(env["sessionmaker"], claim_id) == [
        (str(comp.comparison_id), "supports")
    ]
    assert len(await _evidence_link_rows(env["sessionmaker"], claim_id)) == 4


async def test_comparison_change_creates_new_claim(env) -> None:
    """comparison 变化（新 comparison）→ 新 fingerprint → 新 Claim，旧 Claim 保留。"""
    comp_a = await _seed_comparison(env)
    first = await _service(env).create_claim(_claim_draft(env, supports=[comp_a.comparison_id]))
    comp_b = await _seed_comparison(env, metric_code=ValuationMetricCode.PB_MRQ)
    second = await _service(env).create_claim(
        _claim_draft(env, supports=[comp_a.comparison_id, comp_b.comparison_id])
    )
    assert second.claim_id != first.claim_id
    assert second.replayed is False
    assert await _claim_count(env["sessionmaker"]) == 2


async def test_relation_change_creates_new_claim(env) -> None:
    """同一 comparison，relation 变化（supports↔context）→ 新 fingerprint → 新 Claim。"""
    comp = await _seed_comparison(env)
    # claim1：comp supports；claim2：需要至少 1 个 support，用另一 comparison。
    comp2 = await _seed_comparison(env, metric_code=ValuationMetricCode.PB_MRQ)
    first = await _service(env).create_claim(
        _claim_draft(env, supports=[comp.comparison_id], context=[comp2.comparison_id])
    )
    second = await _service(env).create_claim(
        _claim_draft(env, supports=[comp2.comparison_id], context=[comp.comparison_id])
    )
    assert second.claim_id != first.claim_id
    assert second.replayed is False
    assert await _claim_count(env["sessionmaker"]) == 2


async def test_profile_corruption_integrity_error(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    first = await _service(env).create_claim(draft)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE relative_valuation_claim_profiles SET assessment = 'mixed' "
                "WHERE claim_id = :cid"
            ).bindparams(cid=first.claim_id)
        )
        await session.commit()
    with pytest.raises(ValuationClaimIntegrityError, match="profile"):
        await _service(env).create_claim(draft)


async def test_comparison_link_corruption_integrity_error(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    first = await _service(env).create_claim(draft)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_relative_valuation_comparison_links SET relation = 'context' "
                "WHERE claim_id = :cid AND comparison_id = :cmp"
            ).bindparams(cid=first.claim_id, cmp=comp.comparison_id)
        )
        await session.commit()
    with pytest.raises(ValuationClaimIntegrityError, match="comparison links"):
        await _service(env).create_claim(draft)


async def test_evidence_link_corruption_integrity_error(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    first = await _service(env).create_claim(draft)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE claim_evidence_links SET relation = 'supports' WHERE claim_id = :cid"
            ).bindparams(cid=first.claim_id)
        )
        await session.commit()
    with pytest.raises(ValuationClaimIntegrityError, match="links"):
        await _service(env).create_claim(draft)


async def test_no_evidence_card_or_comparison_modified(env) -> None:
    comp = await _seed_comparison(env)
    draft = _claim_draft(env, supports=[comp.comparison_id])
    async with env["sessionmaker"]() as session:
        cards_before = (
            await session.execute(
                text("SELECT evidence_card_id FROM evidence_cards ORDER BY evidence_card_id")
            )
        ).all()
        comps_before = (
            await session.execute(
                text(
                    "SELECT comparison_id, comparison_fingerprint "
                    "FROM relative_valuation_comparisons ORDER BY comparison_id"
                )
            )
        ).all()
    await _service(env).create_claim(draft)
    await _service(env).create_claim(draft)  # replay
    async with env["sessionmaker"]() as session:
        cards_after = (
            await session.execute(
                text("SELECT evidence_card_id FROM evidence_cards ORDER BY evidence_card_id")
            )
        ).all()
        comps_after = (
            await session.execute(
                text(
                    "SELECT comparison_id, comparison_fingerprint "
                    "FROM relative_valuation_comparisons ORDER BY comparison_id"
                )
            )
        ).all()
    assert cards_after == cards_before
    assert comps_after == comps_before


# ---------------------------------------------------------------- batch


async def test_create_claim_batch_creates_two_claims_ordered(env) -> None:
    comp_a = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    comp_b = await _seed_comparison(env, metric_code=ValuationMetricCode.PB_MRQ)
    draft_a = _claim_draft(env, supports=[comp_a.comparison_id], statement="结论A。")
    draft_b = _claim_draft(env, supports=[comp_b.comparison_id], statement="结论B。")

    batch: ValuationClaimBatchResult = await _service(env).create_claim_batch([draft_a, draft_b])

    assert [item.ordinal for item in batch.items] == [1, 2]
    assert len(batch.claim_ids) == 2
    assert batch.created_count == 2
    assert batch.replayed_count == 0
    async with env["sessionmaker"]() as session:
        statements = []
        for claim_id in batch.claim_ids:
            claim = await ClaimRepository(session).get_by_id(claim_id)
            statements.append(claim.statement)
    assert statements == ["结论A。", "结论B。"]


async def test_create_claim_batch_mixed_replay_and_create_ordered(env) -> None:
    comp_a = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    comp_b = await _seed_comparison(env, metric_code=ValuationMetricCode.PB_MRQ)
    draft_a = _claim_draft(env, supports=[comp_a.comparison_id], statement="结论A。")
    draft_b = _claim_draft(env, supports=[comp_b.comparison_id], statement="结论B。")

    svc = _service(env)
    first = await svc.create_claim(draft_a)
    batch = await svc.create_claim_batch([draft_a, draft_b])

    assert batch.claim_ids[0] == first.claim_id
    assert batch.items[0].replayed is True
    assert batch.items[1].replayed is False
    assert batch.created_count == 1
    assert batch.replayed_count == 1
    assert await _claim_count(env["sessionmaker"]) == 2


async def test_create_claim_batch_all_or_nothing(env) -> None:
    """batch 中任一 draft 失效（comparison 缺失）→ 整批拒绝，0 写（draft1 也不落库）。"""
    comp_a = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    ghost = uuid4()
    draft_a = _claim_draft(env, supports=[comp_a.comparison_id], statement="结论A。")
    draft_b = _claim_draft(env, supports=[ghost], statement="结论B。")

    with pytest.raises(ValuationClaimComparisonNotFound):
        await _service(env).create_claim_batch([draft_a, draft_b])
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_create_claim_batch_rejects_out_of_range(env) -> None:
    comp = await _seed_comparison(env)
    svc = _service(env)
    with pytest.raises(ValuationClaimDraftError):
        await svc.create_claim_batch([])
    draft = _claim_draft(env, supports=[comp.comparison_id])
    with pytest.raises(ValuationClaimDraftError):
        await svc.create_claim_batch([draft] * (MAX_VALUATION_CLAIMS_PER_BATCH + 1))
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- E2E provenance


async def test_claim_e2e_provenance_trace(env) -> None:
    comp = await _seed_comparison(env)
    result = await _service(env).create_claim(_claim_draft(env, supports=[comp.comparison_id]))

    async with env["sessionmaker"]() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT c.claim_schema_version, c.claim_kind, "
                        "       cl.relation AS comp_rel, cl.comparison_id, "
                        "       rc.target_company_id, rc.target_observation_id, "
                        "       rc.metric_code, rc.peer_count, "
                        "       o.source_evidence_card_id, o.company_id AS obs_company, "
                        "       ec.evidence_card_id AS card_id, ec.company_id AS ec_company, "
                        "       ec.source_id, sr.company_id AS src_company "
                        "FROM claims c "
                        "JOIN claim_relative_valuation_comparison_links cl "
                        "  ON cl.claim_id = c.claim_id "
                        "JOIN relative_valuation_comparisons rc "
                        "  ON rc.comparison_id = cl.comparison_id "
                        "JOIN valuation_metric_observations o "
                        "  ON o.valuation_observation_id = rc.target_observation_id "
                        "JOIN evidence_cards ec "
                        "  ON ec.evidence_card_id = o.source_evidence_card_id "
                        "JOIN source_records sr ON sr.source_id = ec.source_id "
                        "WHERE c.claim_id = :cid"
                    ).bindparams(cid=result.claim_id)
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 1
    r = rows[0]
    assert r["claim_schema_version"] == VALUATION_CLAIM_SCHEMA_VERSION
    assert r["claim_kind"] == "relative_valuation"
    assert r["comp_rel"] == "supports"
    assert r["comparison_id"] == comp.comparison_id
    assert r["target_company_id"] == env["target_company_id"]
    assert r["metric_code"] == "pe_ttm"
    assert r["peer_count"] == 3
    assert r["obs_company"] == env["target_company_id"]
    assert r["ec_company"] == env["target_company_id"]
    assert r["src_company"] == env["target_company_id"]


async def test_claim_peer_provenance_trace(env) -> None:
    """peer 链：claim → link → comparison → peer link → peer observation →
    evidence → source（peer 公司一致）。"""
    comp = await _seed_comparison(env)
    result = await _service(env).create_claim(_claim_draft(env, supports=[comp.comparison_id]))

    async with env["sessionmaker"]() as session:
        peer_rows = (
            await session.execute(
                text(
                    "SELECT pp.peer_company_id, pp.peer_observation_id, "
                    "       o.source_evidence_card_id, ec.company_id AS ec_company, "
                    "       sr.company_id AS src_company "
                    "FROM claim_relative_valuation_comparison_links cl "
                    "JOIN relative_valuation_comparisons rc ON rc.comparison_id = cl.comparison_id "
                    "JOIN relative_valuation_comparison_peers pp "
                    "  ON pp.comparison_id = rc.comparison_id "
                    "JOIN valuation_metric_observations o "
                    "  ON o.valuation_observation_id = pp.peer_observation_id "
                    "JOIN evidence_cards ec "
                    "  ON ec.evidence_card_id = o.source_evidence_card_id "
                    "JOIN source_records sr ON sr.source_id = ec.source_id "
                    "WHERE cl.claim_id = :cid"
                ).bindparams(cid=result.claim_id)
            )
        ).all()
    assert len(peer_rows) == 3
    for r in peer_rows:
        assert r[0] == r[3] == r[4]  # peer company == evidence company == source company
