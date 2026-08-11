"""FinancialClaimService integration tests (stage 4B.2C.1, spec N).

需要真实 PostgreSQL（127.0.0.1:5433）。Observation 行用
`compute_metric_fingerprint` 生成 64-hex 指纹直接插入（满足全部 CK 约束，镜像
migration 0020 guard 的 seed 模式）；company / evidence_card 用真实
`_seed_document_claim` / `_seed_html_source` 服务链；Calculation 用真实
FinancialCalculationService 创建。**零 Chroma / 零 LLM / 零 LangGraph / 零
Report / 零 Audit**。

覆盖（4B.2C.1）：
- 创建：Claim + ClaimEvidenceLink（自动展开 source Evidence）+
  ClaimFinancialCalculationLink 原子落库；claim_schema_version=2；
- 拒绝：无 support Calculation / Calculation 缺失 / Calculation 跨公司 /
  additional Evidence 缺失 / relation conflict（多 Calculation 共享证据跨
  relation / additional 与自动展开冲突）/ critical 缺 eligible 支持；
- replay：同 fingerprint 复用同一行 / 并发 → 1 / Calculation 变化 → 新 Claim /
  relation 变化 → 新 Claim；schema v1 通用 Claim replay 不受影响；
- integrity：篡改 Calculation → FinancialClaimIntegrityError，**不自动 repair**；
  EvidenceCard / FinancialCalculation 行永远不被改写；
- E2E provenance：Claim → ClaimFinancialCalculationLink → FinancialCalculation
  → FinancialCalculationInput → FinancialMetricObservation → EvidenceCard →
  Source；
- 边界：claim_financial_calculation_links 存在 / 未来阶段（5E+）表不得存在；
  Service 只持有 sessionmaker。
"""

import asyncio
from datetime import date
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.claims.contracts import (
    CLAIM_SCHEMA_VERSION,
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimImportance,
    ClaimKind,
    compute_research_question_sha256,
)
from app.claims.financial_contracts import (
    FINANCIAL_CLAIM_SCHEMA_VERSION,
    FinancialClaimConfidence,
    FinancialClaimDraft,
    FinancialClaimImportance,
)
from app.claims.financial_errors import (
    FinancialClaimCalculationMismatch,
    FinancialClaimCalculationNotFound,
    FinancialClaimCriticalEvidenceInsufficient,
    FinancialClaimDraftError,
    FinancialClaimEvidenceCompanyMismatch,
    FinancialClaimIntegrityError,
    FinancialClaimRelationConflict,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
from app.financial.calculations.contracts import (
    CalculationCode,
    FinancialCalculationDraft,
    InputRole,
)
from app.financial.calculations.service import FinancialCalculationService
from app.financial.contracts import FINANCIAL_METRIC_SCHEMA_VERSION, compute_metric_fingerprint
from app.repositories.claim_financial_calculation_link_repository import (
    ClaimFinancialCalculationLinkRepository,
)
from app.repositories.claim_repository import ClaimRepository
from app.repositories.company_repository import CompanyRepository
from app.services.claim_service import ClaimService
from app.services.evidence_card_service import EvidenceCardService
from app.services.financial_claim_service import (
    MAX_FINANCIAL_CLAIMS_PER_BATCH,
    FinancialClaimBatchResult,
    FinancialClaimService,
)
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_migration_0018_downgrade_guard import _seed_document_claim

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "2024年贵州茅台净利润增长情况？"
_STATEMENT = "2024年贵州茅台归属净利润同比增长15%。"
_URL = "https://www.xinhuanet.com/2026/0809/0001.htm"
_URL_2 = "https://www.xinhuanet.com/2026/0809/0002.htm"


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
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
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
    await _cleanup(sessionmaker)
    raw_store = None
    seeded = await _seed_document_claim(get_settings().database_url, tmp_path / "raw")
    card_id = UUID(seeded["evidence_card_id"])
    async with sessionmaker() as session:
        company_id = (
            await session.execute(
                text(
                    "SELECT company_id FROM evidence_cards WHERE evidence_card_id = :eid"
                ).bindparams(eid=card_id)
            )
        ).scalar_one()
    from app.storage.raw_store import LocalRawArtifactStore

    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "evidence_card_id": card_id,
    }
    await _cleanup(sessionmaker)


async def _seed_other_company(sessionmaker) -> UUID:
    """另一家 A 股公司（Claim 绑定错误公司的场景）。"""
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


async def _seed_card(
    env: dict,
    *,
    critical_claim_eligible: bool = False,
    statement: str = "2024年贵州茅台营收同比增长15%。",
    source_url: str = _URL_2,
) -> UUID:
    """真实 HTML 链 → EvidenceCardService 创建一张 document EvidenceCard。

    返回 evidence_card_id。
    """
    src, parsed_id, cs_id, chunks = await _seed_html_source(
        env,
        critical_claim_eligible=critical_claim_eligible,
        source_url=source_url,
    )
    chunk = chunks[0]
    draft = EvidenceCardDraft(
        research_question=_QUESTION,
        evidence_statement=statement,
        evidence_type=EvidenceType.METRIC,
        chunk_id=chunk.chunk_id,
        quote_start=0,
        quote_end=20,
        extractor_name="test-extractor",
        extractor_version=1,
        extractor_model_id="test-model",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    result = await EvidenceCardService(env["sessionmaker"]).create_card(draft)
    return result.evidence_card_id


async def _insert_observation(
    env: dict,
    *,
    metric_code: str,
    normalized: str,
    period_start: date | None,
    period_end: date,
    period_kind: str,
    source_card_id: UUID | None = None,
    scope: str = "consolidated",
    raw_unit: str = "yuan",
) -> UUID:
    """直接插入一行满足全部 CK 约束的 FinancialMetricObservation（fingerprint 用
    生产函数生成；镜像 migration 0020 guard 的 seed 模式）。"""
    company_id = env["company_id"]
    card_id = source_card_id if source_card_id is not None else env["evidence_card_id"]
    fingerprint = compute_metric_fingerprint(
        metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
        company_id=company_id,
        source_evidence_card_id=card_id,
        metric_code=metric_code,
        statement_scope=scope,
        period_start=period_start,
        period_end=period_end,
        period_kind=period_kind,
        source_value_text="123",
        raw_value=Decimal(normalized),
        raw_unit=raw_unit,
        normalized_value_cny=Decimal(normalized),
    )
    obs_id = uuid4()
    async with env["sessionmaker"]() as session:
        session.add(
            FinancialMetricObservationModel(
                metric_observation_id=obs_id,
                company_id=company_id,
                source_evidence_card_id=card_id,
                metric_code=metric_code,
                statement_scope=scope,
                period_start=period_start,
                period_end=period_end,
                period_kind=period_kind,
                source_value_text="123",
                raw_value=Decimal(normalized),
                raw_unit=raw_unit,
                normalized_value_cny=Decimal(normalized),
                metric_schema_version=FINANCIAL_METRIC_SCHEMA_VERSION,
                metric_fingerprint=fingerprint,
            )
        )
        await session.commit()
    return obs_id


async def _annual_revenue_pair(env: dict, **kwargs) -> dict:
    """2024 / 2023 全年营收观察（duration，consecutive annual）。"""
    current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="12000000000",
        **kwargs,
    )
    baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="10000000000",
        **kwargs,
    )
    return {InputRole.CURRENT: current, InputRole.BASELINE: baseline}


async def _calc(env: dict, obs: dict, code: CalculationCode = CalculationCode.ABSOLUTE_CHANGE_CNY):
    """用 FinancialCalculationService 创建真实 Calculation，返回 result。"""
    draft = FinancialCalculationDraft(
        company_id=env["company_id"],
        calculation_code=code,
        input_observation_ids={role: oid for role, oid in obs.items()},
    )
    return await FinancialCalculationService(env["sessionmaker"]).create_calculation(draft)


def _fin_draft(
    env: dict,
    *,
    supports=(),
    contradicts=(),
    context=(),
    add_supports=(),
    add_contradicts=(),
    add_context=(),
    importance=FinancialClaimImportance.NORMAL,
    claim_kind=ClaimKind.FACT,
    statement=_STATEMENT,
    **overrides,
) -> FinancialClaimDraft:
    values = dict(
        company_id=env["company_id"],
        research_question=_QUESTION,
        statement=statement,
        confidence=FinancialClaimConfidence.HIGH,
        importance=importance,
        claim_kind=claim_kind,
        support_calculation_ids=list(supports),
        contradict_calculation_ids=list(contradicts),
        context_calculation_ids=list(context),
        additional_support_evidence_ids=list(add_supports),
        additional_contradict_evidence_ids=list(add_contradicts),
        additional_context_evidence_ids=list(add_context),
        analyst_name="structured-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    values.update(overrides)
    return FinancialClaimDraft(**values)


async def _create_fin(env: dict, draft: FinancialClaimDraft):
    return await FinancialClaimService(env["sessionmaker"]).create_claim(draft)


async def _fin_claim_count(sessionmaker) -> int:
    """financial Claim（claim_schema_version >= 2）数量。

    `_seed_document_claim` 会 seed 一条 v1 通用 Claim，因此只统计 v2/v3，
    排除种子。
    """
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM claims WHERE claim_schema_version >= 2")
                )
            ).scalar_one()
        )


async def _fin_link_rows(sessionmaker, claim_id: UUID) -> list[tuple[str, str]]:
    async with sessionmaker() as session:
        links = await ClaimFinancialCalculationLinkRepository(session).list_by_claim(claim_id)
        return sorted((str(link.calculation_id), link.relation) for link in links)


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


# ---------------------------------------------------------------- 创建


async def test_create_financial_claim_persists_claim_evidence_calc_links(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    result = await _create_fin(env, _fin_draft(env, supports=[calc.calculation_id]))

    assert result.replayed is False
    assert len(result.claim_fingerprint) == 64
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.company_id == env["company_id"]
    assert claim.analysis_domain == "financial"
    assert claim.claim_kind == "fact"
    assert claim.claim_schema_version == FINANCIAL_CLAIM_SCHEMA_VERSION
    assert claim.claim_fingerprint == result.claim_fingerprint
    # ClaimFinancialCalculationLink：support calc 落库。
    assert await _fin_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(calc.calculation_id), "supports")
    ]
    # ClaimEvidenceLink：v3 下自动展开 calc 的 source Evidence 一律 relation=context
    # （Calculation 承担 supports/contradicts/context 语义，source Evidence 只提供
    # provenance context）。
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(env["evidence_card_id"]), "context")
    ]


async def test_financial_claim_requires_support_calculation(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    with pytest.raises(FinancialClaimDraftError, match="support_calculation_id"):
        FinancialClaimDraft(
            company_id=env["company_id"],
            research_question=_QUESTION,
            statement=_STATEMENT,
            confidence=FinancialClaimConfidence.HIGH,
            importance=FinancialClaimImportance.NORMAL,
            claim_kind=ClaimKind.FACT,
            support_calculation_ids=[],
            contradict_calculation_ids=[],
            context_calculation_ids=[calc.calculation_id],
            additional_support_evidence_ids=[],
            additional_contradict_evidence_ids=[],
            additional_context_evidence_ids=[],
            analyst_name="structured-analyst",
            analyst_version=1,
            analyst_model_id="deepseek:deepseek-v4-flash",
        )
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_calculation_missing_rejected(env) -> None:
    ghost = uuid4()
    with pytest.raises(FinancialClaimCalculationNotFound):
        await _create_fin(env, _fin_draft(env, supports=[ghost]))
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_calculation_company_mismatch_rejected(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    other_company = await _seed_other_company(env["sessionmaker"])
    draft = _fin_draft(env, supports=[calc.calculation_id], company_id=other_company)
    with pytest.raises(FinancialClaimCalculationMismatch):
        await _create_fin(env, draft)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_calculation_corruption_rejected_no_repair(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    draft = _fin_draft(env, supports=[calc.calculation_id])
    await _create_fin(env, draft)

    # 篡改上游 Calculation 的 result_value → 重新 create 时 Calculation replay 失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_calculations SET result_value = 1 WHERE calculation_id = :cid"
            ).bindparams(cid=calc.calculation_id)
        )
        await session.commit()

    with pytest.raises(FinancialClaimIntegrityError):
        await _create_fin(env, draft)
    # 不自动 repair：篡改值仍在。
    async with env["sessionmaker"]() as session:
        value = (
            await session.execute(
                text(
                    "SELECT result_value FROM financial_calculations WHERE calculation_id = :cid"
                ).bindparams(cid=calc.calculation_id)
            )
        ).scalar_one()
    assert Decimal(str(value)) == Decimal("1")
    assert await _fin_claim_count(env["sessionmaker"]) == 1  # 既有 financial Claim 保留


# ---------------------------------------------------------------- 自动展开 / relation


async def test_automatic_evidence_expansion_dedupes_across_calculations(env) -> None:
    """两个 Calculations 共享同一 source Evidence 且同 relation → Evidence link
    去重为 1；Calculation links 保留 2。"""
    obs_a = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs_a)
    # 第二个 calc 用不同 baseline 数值（不同 fingerprint），但 source card 相同。
    new_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="9000000000",
    )
    obs_b = {**obs_a, InputRole.BASELINE: new_baseline}
    calc_b = await _calc(env, obs_b)

    result = await _create_fin(
        env, _fin_draft(env, supports=[calc_a.calculation_id, calc_b.calculation_id])
    )
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(env["evidence_card_id"]), "context")
    ]
    assert await _fin_link_rows(env["sessionmaker"], result.claim_id) == sorted(
        [
            (str(calc_a.calculation_id), "supports"),
            (str(calc_b.calculation_id), "supports"),
        ]
    )


async def test_v2_conflicting_propagated_relation_rejected(env) -> None:
    """v2（legacy relation propagation）：同一 source Evidence 被两个 Calculations
    推导成不同 relation → 拒绝。"""
    obs_a = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs_a)
    # 两个 calc 都用同一 source card（env card），但 obs 数值不同（fingerprint 不同）。
    new_current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="13000000000",
    )
    new_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="9000000000",
    )
    obs_b = {InputRole.CURRENT: new_current, InputRole.BASELINE: new_baseline}
    calc_b = await _calc(env, obs_b)

    draft = _fin_draft(
        env,
        supports=[calc_a.calculation_id],
        contradicts=[calc_b.calculation_id],
        claim_schema_version=2,
    )
    with pytest.raises(FinancialClaimRelationConflict):
        await _create_fin(env, draft)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_v3_shared_source_evidence_never_conflicts(env) -> None:
    """v3：两个 Calculations 共享同一 source Evidence 即使 Calculation relation
    不同（supports + contradicts）也不冲突——source Evidence 一律 context，去重
    为 1 条 context link；Calculation 承担语义 relation。"""
    obs_a = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs_a)
    new_current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="13000000000",
    )
    new_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="9000000000",
    )
    obs_b = {InputRole.CURRENT: new_current, InputRole.BASELINE: new_baseline}
    calc_b = await _calc(env, obs_b)

    result = await _create_fin(
        env,
        _fin_draft(env, supports=[calc_a.calculation_id], contradicts=[calc_b.calculation_id]),
    )
    # source Evidence 去重为 1 条 context link；Calculation links 保留两个 relation。
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(env["evidence_card_id"]), "context")
    ]
    assert await _fin_link_rows(env["sessionmaker"], result.claim_id) == sorted(
        [
            (str(calc_a.calculation_id), "supports"),
            (str(calc_b.calculation_id), "contradicts"),
        ]
    )


async def test_additional_evidence_merged(env) -> None:
    extra = await _seed_card(env)
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    result = await _create_fin(
        env, _fin_draft(env, supports=[calc.calculation_id], add_supports=[extra])
    )
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == sorted(
        [
            (str(env["evidence_card_id"]), "context"),
            (str(extra), "supports"),
        ]
    )
    # additional 只进 evidence links，不进 calculation links。
    assert await _fin_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(calc.calculation_id), "supports")
    ]


async def test_additional_evidence_conflict_rejected(env) -> None:
    """v3：additional Evidence 保持调用方指定 relation；与 automatic source
    Evidence 的 context relation 冲突（additional_supports 指定 source card）→
    拒绝。"""
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    draft = _fin_draft(env, supports=[calc.calculation_id], add_supports=[env["evidence_card_id"]])
    with pytest.raises(FinancialClaimRelationConflict):
        await _create_fin(env, draft)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_additional_evidence_missing_rejected(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    ghost = uuid4()
    with pytest.raises(FinancialClaimEvidenceCompanyMismatch):
        await _create_fin(
            env, _fin_draft(env, supports=[calc.calculation_id], add_supports=[ghost])
        )
    assert await _fin_claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- critical policy


async def test_critical_with_eligible_source_accepted(env) -> None:
    eligible = await _seed_card(env, critical_claim_eligible=True)
    obs = await _annual_revenue_pair(env, source_card_id=eligible)
    calc = await _calc(env, obs)
    result = await _create_fin(
        env,
        _fin_draft(
            env,
            supports=[calc.calculation_id],
            importance=FinancialClaimImportance.CRITICAL,
        ),
    )
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.importance == "critical"
    # v3：source Evidence（eligible）进入 context 链路；critical source policy
    # 由"任一 support Calculation 的 source Evidence eligible"满足。
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(eligible), "context")
    ]


async def test_critical_without_eligible_source_rejected(env) -> None:
    obs = await _annual_revenue_pair(env)  # seed card critical_claim_eligible=False
    calc = await _calc(env, obs)
    with pytest.raises(FinancialClaimCriticalEvidenceInsufficient):
        await _create_fin(
            env,
            _fin_draft(
                env,
                supports=[calc.calculation_id],
                importance=FinancialClaimImportance.CRITICAL,
            ),
        )
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_v3_critical_additional_support_eligible_accepted(env) -> None:
    """v3 critical source policy ②：additional_support_evidence_ids 中存在
    critical_claim_eligible_snapshot=true → accept（即使 support calc 的 source
    不 eligible）。"""
    eligible = await _seed_card(env, critical_claim_eligible=True)
    obs = await _annual_revenue_pair(env)  # env card 不 eligible
    calc = await _calc(env, obs)
    result = await _create_fin(
        env,
        _fin_draft(
            env,
            supports=[calc.calculation_id],
            add_supports=[eligible],
            importance=FinancialClaimImportance.CRITICAL,
        ),
    )
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.importance == "critical"
    # eligible 卡进入 supports；自动展开的 env card 进入 context。
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == sorted(
        [
            (str(env["evidence_card_id"]), "context"),
            (str(eligible), "supports"),
        ]
    )


async def test_v3_critical_context_eligible_does_not_satisfy(env) -> None:
    """v3 critical：eligible Evidence 只在 context（additional_context）不能满足
    critical 要求——v3 只认 support Calculation 的 source Evidence 与
    additional_support_evidence_ids。"""
    eligible = await _seed_card(env, critical_claim_eligible=True)
    obs = await _annual_revenue_pair(env)  # env card 不 eligible
    calc = await _calc(env, obs)
    with pytest.raises(FinancialClaimCriticalEvidenceInsufficient):
        await _create_fin(
            env,
            _fin_draft(
                env,
                supports=[calc.calculation_id],
                add_context=[eligible],
                importance=FinancialClaimImportance.CRITICAL,
            ),
        )
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_v2_critical_legacy_supports_links_policy(env) -> None:
    """v2（legacy）：critical 检查最终 supports evidence links（relation
    propagation 使 source Evidence 进入 supports）中任一 eligible。"""
    eligible = await _seed_card(env, critical_claim_eligible=True)
    obs = await _annual_revenue_pair(env, source_card_id=eligible)
    calc = await _calc(env, obs)
    result = await _create_fin(
        env,
        _fin_draft(
            env,
            supports=[calc.calculation_id],
            importance=FinancialClaimImportance.CRITICAL,
            claim_schema_version=2,
        ),
    )
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.claim_schema_version == 2
    assert claim.importance == "critical"
    # v2 propagation：source Evidence（eligible）进入 supports links。
    assert await _evidence_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(eligible), "supports")
    ]


async def test_v2_critical_rejects_without_eligible_supports(env) -> None:
    """v2（legacy）：supports links 中无 eligible → reject（与 v3 一致，但走旧
    policy 代码路径）。"""
    obs = await _annual_revenue_pair(env)  # env card 不 eligible
    calc = await _calc(env, obs)
    with pytest.raises(FinancialClaimCriticalEvidenceInsufficient):
        await _create_fin(
            env,
            _fin_draft(
                env,
                supports=[calc.calculation_id],
                importance=FinancialClaimImportance.CRITICAL,
                claim_schema_version=2,
            ),
        )
    assert await _fin_claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- replay / 并发


async def test_financial_claim_fingerprint_deterministic(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    draft = _fin_draft(env, supports=[calc.calculation_id])
    first = await _create_fin(env, draft)
    second = await _create_fin(env, draft)
    assert first.claim_id == second.claim_id
    assert first.claim_fingerprint == second.claim_fingerprint
    assert second.replayed is True
    assert await _fin_claim_count(env["sessionmaker"]) == 1


async def test_calculation_change_creates_new_financial_claim(env) -> None:
    obs_a = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs_a)
    first = await _create_fin(env, _fin_draft(env, supports=[calc_a.calculation_id]))

    new_current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="13000000000",
    )
    obs_b = {InputRole.CURRENT: new_current, InputRole.BASELINE: obs_a[InputRole.BASELINE]}
    calc_b = await _calc(env, obs_b)
    second = await _create_fin(env, _fin_draft(env, supports=[calc_b.calculation_id]))

    assert second.claim_id != first.claim_id
    assert second.replayed is False
    assert await _fin_claim_count(env["sessionmaker"]) == 2  # 旧 Claim 保留


async def test_relation_change_creates_new_financial_claim(env) -> None:
    """同一对 Calculations，relation 交换（supports↔context）→ 新 fingerprint → 新 Claim。

    financial Claim 至少需要 1 个 support Calculation，因此用两个 Calculations
    交换 roles 来验证 relation 变化产生新 Claim。
    """
    obs_a = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs_a)
    # calc_b 用**另一张** EvidenceCard 作为 source（避免与 calc_a 共享 source
    # Evidence → relation 交换时才不会触发自动展开 conflict）。
    card_b = await _seed_card(env)
    obs_b_current = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2024, 1, 1),
        period_end=date(2024, 12, 31),
        period_kind="duration",
        normalized="13000000000",
        source_card_id=card_b,
    )
    obs_b_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="9000000000",
        source_card_id=card_b,
    )
    calc_b = await _calc(
        env,
        {InputRole.CURRENT: obs_b_current, InputRole.BASELINE: obs_b_baseline},
    )
    first = await _create_fin(
        env,
        _fin_draft(
            env,
            supports=[calc_a.calculation_id],
            context=[calc_b.calculation_id],
        ),
    )
    second = await _create_fin(
        env,
        _fin_draft(
            env,
            supports=[calc_b.calculation_id],
            context=[calc_a.calculation_id],
        ),
    )

    assert second.claim_id != first.claim_id
    assert second.replayed is False
    assert await _fin_claim_count(env["sessionmaker"]) == 2


async def test_concurrent_create_yields_single_financial_claim(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    draft = _fin_draft(env, supports=[calc.calculation_id])
    service = FinancialClaimService(env["sessionmaker"])
    results = await asyncio.gather(*(service.create_claim(draft) for _ in range(5)))
    ids = {r.claim_id for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r.replayed) == 4
    assert await _fin_claim_count(env["sessionmaker"]) == 1
    claim_id = next(iter(ids))
    assert await _fin_link_rows(env["sessionmaker"], claim_id) == [
        (str(calc.calculation_id), "supports")
    ]
    assert await _evidence_link_rows(env["sessionmaker"], claim_id) == [
        (str(env["evidence_card_id"]), "context")
    ]


async def test_schema_v1_generic_claim_replay_unchanged(env) -> None:
    """新增 v2 fingerprint 不影响既有 v1 通用 Claim 的 replay。"""
    card = env["evidence_card_id"]
    draft = ClaimDraft(
        company_id=env["company_id"],
        research_question=_QUESTION,
        statement=_STATEMENT,
        analysis_domain=ClaimAnalysisDomain.FINANCIAL,
        claim_kind=ClaimKind.FACT,
        confidence=ClaimConfidence.HIGH,
        importance=ClaimImportance.NORMAL,
        support_evidence_ids=[card],
        contradict_evidence_ids=[],
        context_evidence_ids=[],
        analyst_name="structured-analyst",
        analyst_version=1,
        analyst_model_id="deepseek:deepseek-v4-flash",
    )
    service = ClaimService(env["sessionmaker"])
    first = await service.create_claim(draft)
    second = await service.create_claim(draft)
    assert first.claim_id == second.claim_id
    assert second.replayed is True
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(first.claim_id)
    assert claim is not None
    assert claim.claim_schema_version == CLAIM_SCHEMA_VERSION  # 1
    # financial Claim 单独用 v2。
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    fin = await _create_fin(env, _fin_draft(env, supports=[calc.calculation_id]))
    async with env["sessionmaker"]() as session:
        fin_claim = await ClaimRepository(session).get_by_id(fin.claim_id)
    assert fin_claim is not None
    assert fin_claim.claim_schema_version == FINANCIAL_CLAIM_SCHEMA_VERSION  # 3


async def test_v2_financial_claim_replay_compatible(env) -> None:
    """v2 已落地 Claim 必须继续可读/replay：以 claim_schema_version=2 创建 →
    再次同 draft 创建 → replay 同一行，schema_version=2 原样保留。"""
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    draft = _fin_draft(env, supports=[calc.calculation_id], claim_schema_version=2)
    first = await _create_fin(env, draft)
    second = await _create_fin(env, draft)
    assert first.claim_id == second.claim_id
    assert second.replayed is True
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(first.claim_id)
    assert claim is not None
    assert claim.claim_schema_version == 2
    # v2 propagation：source Evidence 进入 supports links。
    assert await _evidence_link_rows(env["sessionmaker"], first.claim_id) == [
        (str(env["evidence_card_id"]), "supports")
    ]


async def test_v2_v3_schema_versions_do_not_collide(env) -> None:
    """同一 draft（仅 claim_schema_version 不同）→ 不同 fingerprint → 两行，
    不错误 collision。"""
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    v2 = await _create_fin(
        env, _fin_draft(env, supports=[calc.calculation_id], claim_schema_version=2)
    )
    v3 = await _create_fin(env, _fin_draft(env, supports=[calc.calculation_id]))
    assert v2.claim_id != v3.claim_id
    assert v2.claim_fingerprint != v3.claim_fingerprint
    assert v2.replayed is False
    assert v3.replayed is False
    assert await _fin_claim_count(env["sessionmaker"]) == 2
    async with env["sessionmaker"]() as session:
        v2_claim = await ClaimRepository(session).get_by_id(v2.claim_id)
        v3_claim = await ClaimRepository(session).get_by_id(v3.claim_id)
    assert v2_claim.claim_schema_version == 2
    assert v3_claim.claim_schema_version == 3


# ---------------------------------------------------------------- integrity


_CARD_STATE_SQL = (
    "SELECT evidence_card_id, evidence_statement, quote_text, quote_start, quote_end, "
    "evidence_fingerprint FROM evidence_cards ORDER BY evidence_card_id"
)
_CALC_STATE_SQL = (
    "SELECT calculation_id, result_value, result_unit, calculation_fingerprint "
    "FROM financial_calculations ORDER BY calculation_id"
)


async def test_no_evidence_card_or_calculation_modified(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    async with env["sessionmaker"]() as session:
        cards_before = (await session.execute(text(_CARD_STATE_SQL))).all()
        calcs_before = (await session.execute(text(_CALC_STATE_SQL))).all()
    await _create_fin(env, _fin_draft(env, supports=[calc.calculation_id]))
    await _create_fin(env, _fin_draft(env, supports=[calc.calculation_id]))  # replay
    async with env["sessionmaker"]() as session:
        cards_after = (await session.execute(text(_CARD_STATE_SQL))).all()
        calcs_after = (await session.execute(text(_CALC_STATE_SQL))).all()
    assert len(cards_after) == len(cards_before)  # 不新增 Evidence 行
    assert cards_after == cards_before  # 不改写既有 Evidence 行
    assert len(calcs_after) == len(calcs_before)  # 不新增 Calculation 行
    assert calcs_after == calcs_before  # 不改写既有 Calculation 行


# ---------------------------------------------------------------- E2E 回溯


async def test_financial_claim_e2e_provenance_trace(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    result = await _create_fin(env, _fin_draft(env, supports=[calc.calculation_id]))
    async with env["sessionmaker"]() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT c.claim_schema_version, c.research_question_sha256, "
                        "       cl.relation AS calc_rel, cl.calculation_id, "
                        "       fc.calculation_code, fc.result_value, "
                        "       fci.input_role, fci.metric_observation_id, "
                        "       mo.source_evidence_card_id, mo.company_id AS mo_company, "
                        "       ec.evidence_card_id AS card_id, ec.company_id AS ec_company, "
                        "       ec.source_id, sr.company_id AS src_company "
                        "FROM claims c "
                        "JOIN claim_financial_calculation_links cl ON cl.claim_id = c.claim_id "
                        "JOIN financial_calculations fc ON fc.calculation_id = cl.calculation_id "
                        "JOIN financial_calculation_inputs fci "
                        "  ON fci.calculation_id = fc.calculation_id "
                        "JOIN financial_metric_observations mo "
                        "  ON mo.metric_observation_id = fci.metric_observation_id "
                        "JOIN evidence_cards ec "
                        "  ON ec.evidence_card_id = mo.source_evidence_card_id "
                        "JOIN source_records sr ON sr.source_id = ec.source_id "
                        "WHERE c.claim_id = :cid"
                    ).bindparams(cid=result.claim_id)
                )
            )
            .mappings()
            .all()
        )
    assert len(rows) == 2  # absolute_change 有 current + baseline 两个 inputs
    assert all(r["claim_schema_version"] == FINANCIAL_CLAIM_SCHEMA_VERSION for r in rows)
    assert all(r["calc_rel"] == "supports" for r in rows)
    assert all(r["calculation_id"] == calc.calculation_id for r in rows)
    assert all(r["calculation_code"] == "absolute_change_cny" for r in rows)
    assert all(r["mo_company"] == env["company_id"] for r in rows)
    assert all(r["ec_company"] == env["company_id"] for r in rows)
    assert all(r["src_company"] == env["company_id"] for r in rows)
    assert {r["input_role"] for r in rows} == {"current", "baseline"}
    assert all(r["source_evidence_card_id"] == env["evidence_card_id"] for r in rows)
    assert all(r["card_id"] == env["evidence_card_id"] for r in rows)
    expected_sha = compute_research_question_sha256(_QUESTION)
    assert all(r["research_question_sha256"] == expected_sha for r in rows)


# ---------------------------------------------------------------- 边界


async def test_financial_claim_tables_exist_and_no_stage5_report_tables(env) -> None:
    async with env["sessionmaker"]() as session:
        fin_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' "
                    "AND table_name IN "
                    "('claim_financial_calculation_links','financial_calculation_inputs',"
                    "'financial_calculations','financial_metric_observations')"
                )
            )
        ).scalar_one()
        assert fin_tables == 4
        stage5_tables = (
            await session.execute(
                text(
                    "SELECT count(*) FROM information_schema.tables "
                    "WHERE table_schema='public' AND table_name IN "
                    "('report_sections')"
                )
            )
        ).scalar_one()
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


async def test_financial_claim_service_takes_only_sessionmaker(env) -> None:
    service = FinancialClaimService(env["sessionmaker"])
    assert set(service.__dict__) == {"_sessionmaker"}


# ---------------------------------------------------------------- create_claim_batch


async def _second_calc(env: dict, obs: dict):
    """用不同 baseline 数值创建第二个 Calculation（不同 fingerprint，source card 相同）。"""
    new_baseline = await _insert_observation(
        env,
        metric_code="revenue",
        period_start=date(2023, 1, 1),
        period_end=date(2023, 12, 31),
        period_kind="duration",
        normalized="9000000000",
    )
    obs_b = {**obs, InputRole.BASELINE: new_baseline}
    return await _calc(env, obs_b)


async def test_create_claim_batch_creates_two_claims_ordered(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs)
    calc_b = await _second_calc(env, obs)
    draft_a = _fin_draft(env, supports=[calc_a.calculation_id], statement="结论A。")
    draft_b = _fin_draft(env, supports=[calc_b.calculation_id], statement="结论B。")

    service = FinancialClaimService(env["sessionmaker"])
    batch: FinancialClaimBatchResult = await service.create_claim_batch([draft_a, draft_b])

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
    """混合 draft1 replay + draft2 create 时 items 仍按 drafts 顺序 → [draft1, draft2]。"""
    obs = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs)
    calc_b = await _second_calc(env, obs)
    draft_a = _fin_draft(env, supports=[calc_a.calculation_id], statement="结论A。")
    draft_b = _fin_draft(env, supports=[calc_b.calculation_id], statement="结论B。")

    service = FinancialClaimService(env["sessionmaker"])
    # 先单独创建 draft_a（其 fingerprint 已存在）。
    first = await service.create_claim(draft_a)
    # batch [draft_a（replay）, draft_b（create）] → 顺序仍是 [draft_a_id, draft_b_id]。
    batch = await service.create_claim_batch([draft_a, draft_b])

    assert batch.claim_ids[0] == first.claim_id
    assert batch.items[0].replayed is True
    assert batch.items[1].replayed is False
    assert batch.created_count == 1
    assert batch.replayed_count == 1
    assert await _fin_claim_count(env["sessionmaker"]) == 2


async def test_create_claim_batch_all_or_nothing(env) -> None:
    """batch 中任一 draft 失效（calc 缺失）→ 整批拒绝，0 写（draft1 也不落库）。"""
    obs = await _annual_revenue_pair(env)
    calc_a = await _calc(env, obs)
    ghost = uuid4()
    draft_a = _fin_draft(env, supports=[calc_a.calculation_id], statement="结论A。")
    draft_b = _fin_draft(env, supports=[ghost], statement="结论B。")

    service = FinancialClaimService(env["sessionmaker"])
    with pytest.raises(FinancialClaimCalculationNotFound):
        await service.create_claim_batch([draft_a, draft_b])
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_create_claim_batch_rejects_out_of_range(env) -> None:
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    service = FinancialClaimService(env["sessionmaker"])
    with pytest.raises(FinancialClaimDraftError):
        await service.create_claim_batch([])
    draft = _fin_draft(env, supports=[calc.calculation_id])
    with pytest.raises(FinancialClaimDraftError):
        await service.create_claim_batch([draft] * (MAX_FINANCIAL_CLAIMS_PER_BATCH + 1))
    assert await _fin_claim_count(env["sessionmaker"]) == 0
