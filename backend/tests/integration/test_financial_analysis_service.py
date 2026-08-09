"""FinancialAnalysisService integration tests (stage 4B.2C.2, spec O-Q).

需要真实 PostgreSQL（127.0.0.1:5433）。复用 4B.2C.1 的 seeding helpers：
`_insert_observation`（镜像 migration 0020 guard 的 seed 模式）、
`_annual_revenue_pair` / `_calc`（真实 FinancialCalculationService）与
`_seed_card`（真实 EvidenceCardService 链）。模型一律用
`FakeFinancialAnalysisModel`——**零真实 LLM / 零网络 / 零 Chroma / 零 LangGraph /
零 Report / 零 Audit**。

覆盖（4B.2C.2 E2E）：
- 10 步流程 happy path：Calculation Pack（C1）→ fake 决策（C1 support + E1 context）
  → v3 Claim 落库（analyst 身份固定 / analysis_domain=financial /
  analyst_model_id=model.model_id）+ ClaimFinancialCalculationLink(supports) +
  ClaimEvidenceLink(source 自动 context + additional context)；
- relevant=false → 0 claims；reason_code 透传；
- 上游加载失败（Calculation 缺失 / 跨公司 / 重放损坏 / additional Evidence 缺失）
  → 稳定错误，**不调用 LLM**（fake.calls 为空）；
- numeric-literal guard / 未知 C ref / 跨 relation 冲突 → 整次失败 0 写；
- malformed output → FinancialAnalysisMalformedOutput；provider 失败 →
  FinancialAnalysisModelUnavailable 透传；
- replay：同决策再分析 → replayed_count=1，同 claim_id，无重复行；
- critical Claim 缺 eligible source Evidence → FinancialClaimCriticalEvidenceInsufficient
  （0 写）；
- 模型收到的 Calculation/Evidence Pack 是最小投影（无 UUID / fingerprint /
  observation UUID）；C alias 确定性。
"""

from decimal import Decimal
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.financial.contracts import (
    FINANCIAL_ANALYST_NAME,
    FINANCIAL_ANALYST_VERSION,
    FinancialAnalysisDecision,
    FinancialAnalysisReason,
    FinancialAnalysisRequest,
    FinancialClaimCandidate,
)
from app.analysis.financial.errors import (
    FinancialAnalysisCalculationCompanyMismatch,
    FinancialAnalysisCalculationCorrupted,
    FinancialAnalysisCalculationNotFound,
    FinancialAnalysisClaimKindPolicy,
    FinancialAnalysisEvidenceCompanyMismatch,
    FinancialAnalysisMalformedOutput,
    FinancialAnalysisModelUnavailable,
    FinancialAnalysisNumericLiteralForbidden,
    FinancialAnalysisRelationConflict,
    FinancialAnalysisUnknownRef,
)
from app.analysis.financial.service import FinancialAnalysisService
from app.claims.contracts import ClaimKind
from app.claims.financial_contracts import (
    FINANCIAL_CLAIM_SCHEMA_VERSION,
    FinancialClaimConfidence,
    FinancialClaimDraft,
    FinancialClaimImportance,
)
from app.claims.financial_errors import FinancialClaimCriticalEvidenceInsufficient
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.repositories.claim_repository import ClaimRepository
from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
from tests.integration.test_financial_claim_service import (
    _annual_revenue_pair,
    _calc,
    _evidence_link_rows,
    _fin_claim_count,
    _fin_link_rows,
    _seed_card,
    _seed_other_company,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "公司的经营表现如何？"


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    from app.core.config import get_settings

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
    from app.core.config import get_settings
    from app.storage.raw_store import LocalRawArtifactStore
    from tests.integration.test_migration_0018_downgrade_guard import _seed_document_claim

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
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "evidence_card_id": card_id,
    }
    await _cleanup(sessionmaker)


# ---------------------------------------------------------------- helpers


def _candidate(**overrides) -> FinancialClaimCandidate:
    values = dict(
        statement="营业收入保持增长态势。",
        claim_kind=ClaimKind.INFERENCE,
        confidence=FinancialClaimConfidence.HIGH,
        importance=FinancialClaimImportance.NORMAL,
        support_calculation_refs=["C1"],
        contradict_calculation_refs=[],
        context_calculation_refs=[],
        additional_support_evidence_refs=[],
        additional_contradict_evidence_refs=[],
        additional_context_evidence_refs=[],
    )
    values.update(overrides)
    return FinancialClaimCandidate(**values)


def _decision(**overrides) -> FinancialAnalysisDecision:
    values = dict(relevant=True, claims=[_candidate()], reason_code=None)
    values.update(overrides)
    return FinancialAnalysisDecision(**values)


async def _seed_calc(env: dict):
    """一条 2024/2023 营收 yoy Calculation + 返回 (obs, calc)。"""
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    return obs, calc


# ---------------------------------------------------------------- happy path


async def test_analyze_creates_v3_claim_with_analyst_identity(env) -> None:
    _, calc = await _seed_calc(env)
    add_card = await _seed_card(env, statement="管理层说明营收增长主要来自直销渠道拓展。")
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
        additional_evidence_ids=[add_card],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(
                    support_calculation_refs=["C1"],
                    additional_context_evidence_refs=["E1"],
                )
            ]
        )
    )
    service = FinancialAnalysisService(env["sessionmaker"], model)

    result = await service.analyze(request)

    assert result.relevant is True
    assert result.created_count == 1
    assert result.replayed_count == 0
    assert result.reason_code is None
    assert len(result.claim_ids) == 1

    claim_id = result.claim_ids[0]
    async with env["sessionmaker"]() as session:
        claim = await ClaimRepository(session).get_by_id(claim_id)
    assert claim is not None
    assert claim.company_id == env["company_id"]
    assert claim.analysis_domain == "financial"
    assert claim.claim_kind == "inference"
    assert claim.claim_schema_version == FINANCIAL_CLAIM_SCHEMA_VERSION
    assert claim.analyst_name == FINANCIAL_ANALYST_NAME
    assert claim.analyst_version == FINANCIAL_ANALYST_VERSION
    assert claim.analyst_model_id == model.model_id

    assert await _fin_link_rows(env["sessionmaker"], claim_id) == [
        (str(calc.calculation_id), "supports")
    ]
    # source Evidence（calc 的 source card）自动展开为 context；additional E1 为 context。
    # `_evidence_link_rows` 按 (card_id, relation) 排序，因此期望列表同样排序
    # （env_card / add_card 均为随机 UUID，相对顺序不可预测）。
    assert await _evidence_link_rows(env["sessionmaker"], claim_id) == sorted(
        [
            (str(env["evidence_card_id"]), "context"),
            (str(add_card), "context"),
        ]
    )
    assert await _fin_claim_count(env["sessionmaker"]) == 1


async def test_analyze_model_receives_minimal_pack_projection(env) -> None:
    # yoy 计算 → ratio 结果（0.2 → "20.00%"），便于断言存储表达 / display value。
    from app.financial.calculations.contracts import CalculationCode

    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs, code=CalculationCode.YOY_GROWTH_RATE)
    add_card = await _seed_card(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
        additional_evidence_ids=[add_card],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(
                    support_calculation_refs=["C1"],
                    additional_context_evidence_refs=["E1"],
                )
            ]
        )
    )
    await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)

    assert len(model.calls) == 1
    context, calculation_pack, evidence_pack = model.calls[0]
    assert context.analysis_domain == "financial"
    assert context.research_question == _QUESTION
    # Calculation Pack：C1 必要字段 + display value，无内部字段。
    assert [item.calculation_ref for item in calculation_pack.items] == ["C1"]
    item = calculation_pack.items[0]
    assert item.calculation_code == "yoy_growth_rate"
    # 存储表达（scale 12，0.200000000000），不送换算值。
    assert Decimal(item.result_value) == Decimal("0.2")
    assert item.deterministic_display_value == "20.00%"
    assert item.statement_scope == "consolidated"
    # 模型真正收到的是 item 渲染（prompt builder 只渲染 items，不含 ref→UUID 映射）。
    text_repr = "\n".join(str(item) for item in calculation_pack.items)
    for forbidden in (
        str(calc.calculation_id),
        "fingerprint",
        "metric_observation_id",
        "evidence_card_id",
        "locator",
    ):
        assert forbidden not in text_repr
    # Evidence Pack：E1 → add_card。
    assert [item.evidence_ref for item in evidence_pack.items] == ["E1"]
    assert evidence_pack.ref_to_card_id["E1"] == add_card


# ---------------------------------------------------------------- relevant=false


async def test_analyze_relevant_false_creates_no_claims(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=FinancialAnalysisDecision(
            relevant=False, claims=[], reason_code=FinancialAnalysisReason.NOT_RELEVANT
        )
    )
    result = await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)

    assert result.relevant is False
    assert result.claim_ids == []
    assert result.created_count == 0
    assert result.replayed_count == 0
    assert result.reason_code == FinancialAnalysisReason.NOT_RELEVANT
    assert await _fin_claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- 上游加载失败（不调用 LLM）


async def test_analyze_calculation_missing_aborts_before_llm(env) -> None:
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[uuid4()],
    )
    model = FakeFinancialAnalysisModel(decision=_decision())
    with pytest.raises(FinancialAnalysisCalculationNotFound):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert model.calls == []
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_calculation_company_mismatch_aborts_before_llm(env) -> None:
    _, calc = await _seed_calc(env)
    other_company = await _seed_other_company(env["sessionmaker"])
    request = FinancialAnalysisRequest(
        company_id=other_company,
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(decision=_decision())
    with pytest.raises(FinancialAnalysisCalculationCompanyMismatch):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert model.calls == []


async def test_analyze_calculation_corrupted_aborts_before_llm(env) -> None:
    _, calc = await _seed_calc(env)
    # 篡改上游 Calculation 的 result_value → verify_calculation_integrity 重放失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE financial_calculations SET result_value = 1 WHERE calculation_id = :cid"
            ).bindparams(cid=calc.calculation_id)
        )
        await session.commit()
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(decision=_decision())
    with pytest.raises(FinancialAnalysisCalculationCorrupted):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert model.calls == []
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_missing_additional_evidence_aborts_before_llm(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
        additional_evidence_ids=[uuid4()],
    )
    model = FakeFinancialAnalysisModel(decision=_decision())
    with pytest.raises(FinancialAnalysisEvidenceCompanyMismatch):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert model.calls == []


# ---------------------------------------------------------------- 失败路径（0 写）


async def test_analyze_numeric_literal_guard_aborts_zero_writes(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(claims=[_candidate(statement="营业收入同比增长20%。")])
    )
    with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    # LLM 被调用（guard 在模型输出之后），但 0 写。
    assert len(model.calls) == 1
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_unknown_calc_ref_aborts_zero_writes(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(claims=[_candidate(support_calculation_refs=["C99"])])
    )
    with pytest.raises(FinancialAnalysisUnknownRef):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_cross_relation_conflict_aborts_zero_writes(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(
            claims=[_candidate(support_calculation_refs=["C1"], contradict_calculation_refs=["C1"])]
        )
    )
    with pytest.raises(FinancialAnalysisRelationConflict):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_fact_candidate_rejected_zero_writes(env) -> None:
    """Analyst 不得输出 fact Claim：schema 层拒绝 → MalformedOutput，0 写。"""
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision={
            "relevant": True,
            "reason_code": None,
            "claims": [
                {
                    "statement": "营业收入保持增长态势。",
                    "claim_kind": "fact",
                    "confidence": "high",
                    "importance": "normal",
                    "support_calculation_refs": ["C1"],
                    "contradict_calculation_refs": [],
                    "context_calculation_refs": [],
                    "additional_support_evidence_refs": [],
                    "additional_contradict_evidence_refs": [],
                    "additional_context_evidence_refs": [],
                }
            ],
        }
    )
    with pytest.raises(FinancialAnalysisMalformedOutput):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_chinese_numeric_statement_zero_writes(env) -> None:
    """中文数字表达（两成）→ numeric guard 拒绝，整次失败 0 写。"""
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(claims=[_candidate(statement="营业收入增长两成。")])
    )
    with pytest.raises(FinancialAnalysisNumericLiteralForbidden):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert len(model.calls) == 1
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_kind_policy_defensive_rejects_fact_draft() -> None:
    """defensive 兜底：即使绕过 Pydantic（直接构造 fact FinancialClaimDraft），
    Financial Analysis 路径也拒绝 fact → FinancialAnalysisClaimKindPolicy。

    FinancialClaimDraft（更低层 domain contract）本身仍支持 fact。
    """
    draft = FinancialClaimDraft(
        company_id=uuid4(),
        research_question=_QUESTION,
        statement="营业收入保持增长态势。",
        claim_kind=ClaimKind.FACT,
        confidence=FinancialClaimConfidence.HIGH,
        importance=FinancialClaimImportance.NORMAL,
        support_calculation_ids=[uuid4()],
        contradict_calculation_ids=[],
        context_calculation_ids=[],
        additional_support_evidence_ids=[],
        additional_contradict_evidence_ids=[],
        additional_context_evidence_ids=[],
        analyst_name=FINANCIAL_ANALYST_NAME,
        analyst_version=FINANCIAL_ANALYST_VERSION,
        analyst_model_id="test-model",
    )
    with pytest.raises(FinancialAnalysisClaimKindPolicy):
        FinancialAnalysisService._check_kind_policy([draft])


async def test_analyze_malformed_output_mapped(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    # dict 且 relevant=true 但 claims 为空 → ValidationError → MalformedOutput。
    model = FakeFinancialAnalysisModel(decision={"relevant": True, "claims": []})
    with pytest.raises(FinancialAnalysisMalformedOutput):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_model_unavailable_propagates(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(error=FinancialAnalysisModelUnavailable)
    with pytest.raises(FinancialAnalysisModelUnavailable):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


async def test_analyze_critical_claim_requires_eligible_source(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    # source card 默认 critical_claim_eligible=False → critical Claim 缺 eligible 支持。
    model = FakeFinancialAnalysisModel(
        decision=_decision(claims=[_candidate(importance=FinancialClaimImportance.CRITICAL)])
    )
    with pytest.raises(FinancialClaimCriticalEvidenceInsufficient):
        await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)
    assert await _fin_claim_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- replay / 多 claims


async def test_analyze_replays_same_claim_on_second_run(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(decision=_decision())
    service = FinancialAnalysisService(env["sessionmaker"], model)

    first = await service.analyze(request)
    second = await service.analyze(request)

    assert first.created_count == 1
    assert first.replayed_count == 0
    assert second.created_count == 0
    assert second.replayed_count == 1
    assert second.claim_ids == first.claim_ids
    assert len(model.calls) == 2
    assert await _fin_claim_count(env["sessionmaker"]) == 1


async def test_analyze_multiple_claims_ordered_result(env) -> None:
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(statement="营业收入保持增长态势。"),
                _candidate(
                    statement="盈利能力有所改善。",
                    support_calculation_refs=["C1"],
                ),
            ]
        )
    )
    result = await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)

    assert result.created_count == 2
    assert result.replayed_count == 0
    assert len(result.claim_ids) == 2
    assert len(set(result.claim_ids)) == 2
    assert await _fin_claim_count(env["sessionmaker"]) == 2


async def test_analyze_inference_and_risk_claims_persisted(env) -> None:
    """合法 inference / risk 两类 Claim → 正常落库（kind 边界不误伤合法输出）。"""
    _, calc = await _seed_calc(env)
    request = FinancialAnalysisRequest(
        company_id=env["company_id"],
        research_question=_QUESTION,
        calculation_ids=[calc.calculation_id],
    )
    model = FakeFinancialAnalysisModel(
        decision=_decision(
            claims=[
                _candidate(statement="盈利能力有所改善。", claim_kind=ClaimKind.INFERENCE),
                _candidate(
                    statement="经营利润率存在不确定性。",
                    claim_kind=ClaimKind.RISK,
                ),
            ]
        )
    )
    result = await FinancialAnalysisService(env["sessionmaker"], model).analyze(request)

    assert result.created_count == 2
    kinds = set()
    async with env["sessionmaker"]() as session:
        for claim_id in result.claim_ids:
            claim = await ClaimRepository(session).get_by_id(claim_id)
            assert claim is not None
            kinds.add(claim.claim_kind)
    assert kinds == {"inference", "risk"}


# ---------------------------------------------------------------- smoke cleanup 硬化


async def test_smoke_cleanup_removes_all_scratch_rows(env) -> None:
    """smoke 清理硬化：seed 最小 scratch 链路 → smoke `_cleanup` → 实际查询 0 残留。

    不调用真实 LLM；只验证 smoke 的 cleanup helper 能删净一条完整 scratch 链路
    （company → raw/source/parsed/chunk → evidence card → observation →
    calculation → v1 claim），且 `_residual_counts` 实际查询确认 0。
    """
    from app.cli.smoke_financial_analysis import _cleanup as smoke_cleanup
    from app.cli.smoke_financial_analysis import _residual_counts

    await _seed_calc(env)
    async with env["sessionmaker"]() as session:
        artifact_id = (
            await session.execute(
                text("SELECT artifact_id FROM source_records WHERE company_id = :cid").bindparams(
                    cid=env["company_id"]
                )
            )
        ).scalar_one()
    assert artifact_id is not None

    await smoke_cleanup(
        env["sessionmaker"],
        company_id=env["company_id"],
        artifact_id=artifact_id,
    )

    residual = await _residual_counts(
        env["sessionmaker"],
        company_id=env["company_id"],
        artifact_id=artifact_id,
    )
    assert all(count == 0 for count in residual.values()), residual
