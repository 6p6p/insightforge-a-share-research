"""ValuationAnalysisService integration tests (stage 4C.2B.2, spec G/O/P/Q/R/U)。

需要真实 PostgreSQL（127.0.0.1:5433）。复用 4C.2B.1 的 seeding helpers
（`_seed_company` / `_seed_observation` / `_seed_comparison` 走真实
RelativeValuationComparisonService）。模型一律用 FakeValuationAnalysisModel——
**零真实 LLM / 零网络 / 零 Chroma / 零 LangGraph / 零 Report / 零 Audit**。

覆盖（4C.2B.2 E2E）：
- 10 步流程 happy path：Comparison Pack（V1）→ fake 决策（relative_high, V1
  support）→ 确定性 statement 渲染 → v7 Claim 落库（analyst 身份固定 /
  analysis_domain=valuation / analyst_model_id=model.model_id）+ Profile
  （assessment / analysis_as_of / profile_schema_version=1）+
  ClaimRelativeValuationComparisonLink(supports) + automatic context Evidence
  links（target + 全部 peers 的 source Evidence）；
- relevant=false → 0 claims；reason_code 透传；
- 上游加载失败（Comparison 缺失 / 跨公司 / 重放损坏）→ 稳定错误，**不调用 LLM**
  （fake.calls 为空）；
- 失败路径（0 写）：未知 V ref / 跨 relation / 遗漏 input comparison（no
  cherry-picking）/ direction conflict / mixed insufficient / uncertain
  importance policy / malformed output / provider 失败；
- V alias 确定性：pe_ttm → V1、pb_mrq → V2（metric_code 排序）；模型收到的
  Pack 是最小投影（无 UUID / fingerprint / observation UUID）；
- replay：同决策再分析 → replayed=True，同 claim_id，无重复行。
"""

from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.valuation.contracts import (
    VALUATION_ANALYST_NAME,
    VALUATION_ANALYST_VERSION,
    ValuationAnalysisDecision,
    ValuationAnalysisReason,
    ValuationAnalysisRequest,
)
from app.analysis.valuation.errors import (
    ValuationAnalysisComparisonCompanyMismatch,
    ValuationAnalysisComparisonCorrupted,
    ValuationAnalysisComparisonNotFound,
    ValuationAnalysisComparisonOmitted,
    ValuationAnalysisDirectionConflict,
    ValuationAnalysisMalformedOutput,
    ValuationAnalysisMixedEvidenceInsufficient,
    ValuationAnalysisModelUnavailable,
    ValuationAnalysisRelationConflict,
    ValuationAnalysisUncertainImportancePolicy,
    ValuationAnalysisUnknownRef,
)
from app.analysis.valuation.service import ValuationAnalysisService
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.repositories.claim_repository import ClaimRepository
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.claim_contracts import (
    VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
    VALUATION_CLAIM_SCHEMA_VERSION,
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
    render_valuation_claim_statement,
)
from app.valuation.contracts import ValuationMetricCode
from tests.analysis.valuation.fakes import FakeValuationAnalysisModel
from tests.integration.test_valuation_claim_service import (
    _ANALYSIS_AS_OF,
    _claim_count,
    _cleanup,
    _comp_link_rows,
    _evidence_link_rows,
    _profile_rows,
    _seed_company,
    _seed_comparison,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台当前相对估值水平如何？"


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


# ---------------------------------------------------------------- helpers


def _decision(**overrides) -> ValuationAnalysisDecision:
    values = dict(
        relevant=True,
        assessment=ValuationClaimAssessment.RELATIVE_HIGH,
        confidence=ValuationClaimConfidence.HIGH,
        importance=ValuationClaimImportance.NORMAL,
        support_comparison_refs=["V1"],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=None,
    )
    values.update(overrides)
    return ValuationAnalysisDecision(**values)


def _request(env: dict, comparison_ids: list) -> ValuationAnalysisRequest:
    return ValuationAnalysisRequest(
        company_id=env["target_company_id"],
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        comparison_ids=comparison_ids,
    )


# ---------------------------------------------------------------- happy path


async def test_analyze_creates_v7_claim_with_analyst_identity(env) -> None:
    comp = await _seed_comparison(env)  # pe_ttm，target 15.3 / peers 14.2·15.0·16.0 → premium +0.02
    model = FakeValuationAnalysisModel(decision=_decision())
    service = ValuationAnalysisService(env["sessionmaker"], model)

    result = await service.analyze(_request(env, [comp.comparison_id]))

    assert result.relevant is True
    assert result.claim_id is not None
    assert result.replayed is False
    assert result.assessment == ValuationClaimAssessment.RELATIVE_HIGH
    assert result.reason_code is None

    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim is not None
    assert claim.company_id == env["target_company_id"]
    assert claim.analysis_domain == "valuation"
    assert claim.claim_kind == "relative_valuation"
    assert claim.claim_schema_version == VALUATION_CLAIM_SCHEMA_VERSION
    # LLM 不生成 statement：确定性渲染自 assessment + 实际 selected metric_codes。
    assert claim.statement == render_valuation_claim_statement(
        ValuationClaimAssessment.RELATIVE_HIGH, ("pe_ttm",)
    )
    assert claim.analyst_name == VALUATION_ANALYST_NAME
    assert claim.analyst_version == VALUATION_ANALYST_VERSION
    assert claim.analyst_model_id == model.model_id

    assert await _profile_rows(env["sessionmaker"], result.claim_id) == (
        "relative_high",
        _ANALYSIS_AS_OF,
        VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
    )
    assert await _comp_link_rows(env["sessionmaker"], result.claim_id) == [
        (str(comp.comparison_id), "supports")
    ]
    # automatic context Evidence links：target + 全部 peers 的 source Evidence。
    ev_rows = await _evidence_link_rows(env["sessionmaker"], result.claim_id)
    assert len(ev_rows) == 4
    assert {relation for _, relation in ev_rows} == {"context"}
    assert await _claim_count(env["sessionmaker"]) == 1


async def test_analyze_broadly_in_line_accepted_no_threshold(env) -> None:
    """broadly_in_line 不设 threshold：premium 符号任意都不触发 direction policy。"""
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=_decision(assessment=ValuationClaimAssessment.BROADLY_IN_LINE)
    )
    result = await ValuationAnalysisService(env["sessionmaker"], model).analyze(
        _request(env, [comp.comparison_id])
    )
    assert result.claim_id is not None
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(result.claim_id)
    assert claim.statement == render_valuation_claim_statement(
        ValuationClaimAssessment.BROADLY_IN_LINE, ("pe_ttm",)
    )


async def test_analyze_model_receives_minimal_pack_projection(env) -> None:
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(decision=_decision())
    await ValuationAnalysisService(env["sessionmaker"], model).analyze(
        _request(env, [comp.comparison_id])
    )

    assert len(model.calls) == 1
    context, pack = model.calls[0]
    assert context.research_question == _QUESTION
    assert context.analysis_as_of == _ANALYSIS_AS_OF
    assert [item.valuation_ref for item in pack.items] == ["V1"]
    item = pack.items[0]
    assert item.metric_code == "pe_ttm"
    assert item.target_value == "15.3"
    assert item.peer_median == "15"
    assert item.premium_discount_to_median == "0.02"
    assert item.position_vs_median == "above"
    assert item.deterministic_display_premium == "+2.00%"
    text_repr = "\n".join(str(item) for item in pack.items)
    for forbidden in (
        str(comp.comparison_id),
        "fingerprint",
        "valuation_observation_id",
        "evidence_card_id",
        "locator",
        "chroma",
    ):
        assert forbidden not in text_repr


async def test_analyze_v_alias_orders_by_metric_code(env) -> None:
    """pe_ttm → V1、pb_mrq → V2（metric_code 排序；V alias 确定性）。"""
    pe = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    pb = await _seed_comparison(env, metric_code=ValuationMetricCode.PB_MRQ)
    # 两个 input comparison 都引用（no-cherry-picking 全覆盖）；premium 均正 →
    # relative_high 方向检查通过。
    model = FakeValuationAnalysisModel(decision=_decision(support_comparison_refs=["V1", "V2"]))
    await ValuationAnalysisService(env["sessionmaker"], model).analyze(
        _request(env, [pb.comparison_id, pe.comparison_id])  # 提交顺序与 alias 无关
    )
    assert len(model.calls) == 1
    _, pack = model.calls[0]
    assert [item.valuation_ref for item in pack.items] == ["V1", "V2"]
    assert [item.metric_code for item in pack.items] == ["pe_ttm", "pb_mrq"]
    assert pack.ref_to_comparison_id["V1"] == pe.comparison_id
    assert pack.ref_to_comparison_id["V2"] == pb.comparison_id
    assert await _claim_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- relevant=false


async def test_analyze_relevant_false_creates_no_claims(env) -> None:
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=ValuationAnalysisDecision(
            relevant=False,
            assessment=None,
            confidence=None,
            importance=None,
            support_comparison_refs=[],
            contradict_comparison_refs=[],
            context_comparison_refs=[],
            reason_code=ValuationAnalysisReason.NOT_RELEVANT,
        )
    )
    result = await ValuationAnalysisService(env["sessionmaker"], model).analyze(
        _request(env, [comp.comparison_id])
    )
    assert result.relevant is False
    assert result.claim_id is None
    assert result.replayed is False
    assert result.reason_code == ValuationAnalysisReason.NOT_RELEVANT
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- 上游加载失败（不调用 LLM）


async def test_analyze_comparison_missing_aborts_before_llm(env) -> None:
    model = FakeValuationAnalysisModel(decision=_decision())
    with pytest.raises(ValuationAnalysisComparisonNotFound):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(_request(env, [uuid4()]))
    assert model.calls == []
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_comparison_company_mismatch_aborts_before_llm(env) -> None:
    comp = await _seed_comparison(env)
    other_company = await _seed_company(env["sessionmaker"], "600599")
    request = ValuationAnalysisRequest(
        company_id=other_company,
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        comparison_ids=[comp.comparison_id],
    )
    model = FakeValuationAnalysisModel(decision=_decision())
    with pytest.raises(ValuationAnalysisComparisonCompanyMismatch):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(request)
    assert model.calls == []
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_comparison_corrupted_aborts_before_llm(env) -> None:
    comp = await _seed_comparison(env)
    # 篡改 persisted peer_median → verify_comparison_integrity 重放失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE relative_valuation_comparisons SET peer_median = peer_median + 1 "
                "WHERE comparison_id = :cid"
            ).bindparams(cid=comp.comparison_id)
        )
        await session.commit()
    model = FakeValuationAnalysisModel(decision=_decision())
    with pytest.raises(ValuationAnalysisComparisonCorrupted):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert model.calls == []
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- 失败路径（0 写，LLM 已调用）


async def test_analyze_unknown_ref_aborts_zero_writes(env) -> None:
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(decision=_decision(support_comparison_refs=["V99"]))
    with pytest.raises(ValuationAnalysisUnknownRef):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert len(model.calls) == 1
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_cross_relation_conflict_aborts_zero_writes(env) -> None:
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=_decision(support_comparison_refs=["V1"], contradict_comparison_refs=["V1"])
    )
    with pytest.raises(ValuationAnalysisRelationConflict):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_omitted_input_aborts_zero_writes(env) -> None:
    """no-cherry-picking：2 个 input comparison 只引用 1 个 → ComparisonOmitted。"""
    pe = await _seed_comparison(env, metric_code=ValuationMetricCode.PE_TTM)
    pb = await _seed_comparison(env, metric_code=ValuationMetricCode.PB_MRQ)
    model = FakeValuationAnalysisModel(decision=_decision(support_comparison_refs=["V1"]))
    with pytest.raises(ValuationAnalysisComparisonOmitted):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [pe.comparison_id, pb.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_direction_conflict_aborts_zero_writes(env) -> None:
    """relative_low 但 support premium 为正（+0.02）→ DirectionConflict（0 写）。"""
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=_decision(assessment=ValuationClaimAssessment.RELATIVE_LOW)
    )
    with pytest.raises(ValuationAnalysisDirectionConflict):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_mixed_evidence_insufficient_aborts_zero_writes(env) -> None:
    """mixed 但 support 只有单一正 premium → MixedEvidenceInsufficient（0 写）。"""
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=_decision(assessment=ValuationClaimAssessment.MIXED)
    )
    with pytest.raises(ValuationAnalysisMixedEvidenceInsufficient):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_uncertain_importance_policy_aborts_zero_writes(env) -> None:
    """uncertain + critical → UncertainImportancePolicy（0 写；先于 critical 校验）。"""
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=_decision(
            assessment=ValuationClaimAssessment.UNCERTAIN,
            importance=ValuationClaimImportance.CRITICAL,
        )
    )
    with pytest.raises(ValuationAnalysisUncertainImportancePolicy):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_uncertain_normal_accepted(env) -> None:
    """uncertain + normal → 合法（不确定性判断不能标 critical，但 normal 允许）。"""
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(
        decision=_decision(
            assessment=ValuationClaimAssessment.UNCERTAIN,
            importance=ValuationClaimImportance.NORMAL,
        )
    )
    result = await ValuationAnalysisService(env["sessionmaker"], model).analyze(
        _request(env, [comp.comparison_id])
    )
    assert result.claim_id is not None


async def test_analyze_malformed_output_mapped(env) -> None:
    comp = await _seed_comparison(env)
    # dict 且 relevant=true 但缺 assessment → ValidationError → MalformedOutput。
    model = FakeValuationAnalysisModel(
        decision={"relevant": True, "reason_code": None, "support_comparison_refs": ["V1"]}
    )
    with pytest.raises(ValuationAnalysisMalformedOutput):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


async def test_analyze_model_unavailable_propagates(env) -> None:
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(error=ValuationAnalysisModelUnavailable)
    with pytest.raises(ValuationAnalysisModelUnavailable):
        await ValuationAnalysisService(env["sessionmaker"], model).analyze(
            _request(env, [comp.comparison_id])
        )
    assert await _claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- replay


async def test_analyze_replays_same_claim_on_second_run(env) -> None:
    comp = await _seed_comparison(env)
    model = FakeValuationAnalysisModel(decision=_decision())
    service = ValuationAnalysisService(env["sessionmaker"], model)

    first = await service.analyze(_request(env, [comp.comparison_id]))
    second = await service.analyze(_request(env, [comp.comparison_id]))

    assert first.replayed is False
    assert second.replayed is True
    assert second.claim_id == first.claim_id
    assert len(model.calls) == 2
    assert await _claim_count(env["sessionmaker"]) == 1
