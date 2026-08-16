"""Stage 4 workflow full-chain E2E tests (spec N-P + spec R-S).

真实 PostgreSQL + Fake LLM models + 真实 LangGraph + PG Checkpointer，全程
**零真实 DeepSeek**（Fake 模型都是确定性返回 / 抛错）。

覆盖（spec R-S）：
- full-chain E2E：business / risk / financial / macro / valuation 5 类 work
  item → Send fan-out → 真实 Services 各产 1 条 Claim → collect canonical →
  SynthesisService + SynthesisAnalysisService → 1 SynthesisRun + 1 Result；
- durable resume（spec N-O）：runner A 部分 success → 注入 financial model
  失败（慢失败，确保其余 worker 已完成并 checkpoint）→ run failed；新 runner B
  同 thread_id 恢复 → completed；**无重复业务对象**（Claim / SynthesisRun /
  SynthesisResult 各 1 份），且已完成的 worker 不重跑（模型零调用）；
- events（spec P）：WorkflowEvent 只记录 node / status / item_id /
  analysis_type / counts / business IDs；payload 无 Evidence text / prompt /
  reasoning_content；
- boundary（spec R）：不创建 Stage 5 report / draft / audit 表。
"""

import asyncio
import json
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.claims.contracts import ClaimAnalysisDecision, ClaimCandidate
from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.financial.contracts import FinancialAnalysisDecision, FinancialClaimCandidate
from app.analysis.financial.errors import FinancialAnalysisModelUnavailable
from app.analysis.financial.service import FinancialAnalysisService
from app.analysis.macro.contracts import (
    MacroAnalysisDecision,
    MacroClaimCandidate,
)
from app.analysis.macro.service import MacroAnalysisService
from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisTheme,
)
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.analysis.valuation.contracts import ValuationAnalysisDecision
from app.analysis.valuation.service import ValuationAnalysisService
from app.claims.contracts import ClaimConfidence, ClaimImportance, ClaimKind
from app.claims.financial_contracts import (
    FinancialClaimConfidence,
    FinancialClaimImportance,
)
from app.claims.macro_contracts import (
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.repositories.research_task_repository import ResearchTaskRepository
from app.services.source_registry_service import SourceRegistryService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage4.runner import Stage4WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
)
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.analysis.claims.fakes import FakeClaimAnalysisModel
from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
from tests.analysis.macro.fakes import FakeMacroAnalysisModel
from tests.analysis.synthesis.fakes import FakeSynthesisAnalysisModel
from tests.analysis.valuation.fakes import FakeValuationAnalysisModel
from tests.integration.test_claim_service import (
    _seed_document_card as _seed_claim_doc_card,
)
from tests.integration.test_financial_claim_service import _annual_revenue_pair, _calc
from tests.integration.test_macro_claim_service import (
    _seed_document_card as _seed_macro_doc_card,
)
from tests.integration.test_macro_claim_service import (
    _seed_macro_card,
)
from tests.integration.test_valuation_claim_service import _seed_company, _seed_comparison

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)

_URL_B = "https://www.xinhuanet.com/2026/0809/s4biz.htm"
_URL_R = "https://www.xinhuanet.com/2026/0809/s4rsk.htm"


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM workflow_events"))
        await session.execute(text("DELETE FROM workflow_runs"))
        await session.execute(text("DELETE FROM research_tasks"))
        await session.execute(text("DELETE FROM draft_sections"))
        await session.execute(text("DELETE FROM report_outlines"))
        await session.execute(text("DELETE FROM claim_synthesis_results"))
        await session.execute(text("DELETE FROM claim_synthesis_input_links"))
        await session.execute(text("DELETE FROM claim_synthesis_runs"))
        await session.execute(text("DELETE FROM claim_relative_valuation_comparison_links"))
        await session.execute(text("DELETE FROM relative_valuation_claim_profiles"))
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM macro_transmission_evidence_links"))
        await session.execute(text("DELETE FROM macro_transmission_chains"))
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
async def connection_uri() -> str:
    return to_postgres_connection_uri(get_settings().database_url)


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    peer_company_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    task_id = await _seed_research_task(sessionmaker)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "target_company_id": company_id,
        "peer_company_ids": peer_company_ids,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


async def _seed_research_task(sessionmaker) -> UUID:
    """seed 一个真实 ResearchTask（Stage 4 WorkflowRun 必须绑定任务）。"""
    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=date(2026, 12, 31),
                modules=["company_profile"],
                questions=[],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


# ---------------------------------------------------------------- worker inputs


async def _seed_worker_inputs(
    env: dict, monkeypatch, *, research_question: str = _QUESTION
) -> dict:
    """seed 5 类 worker 的输入，返回 (item 构造所需 IDs)。

    - business / risk：document EvidenceCard（ClaimAnalysisService）；
    - financial：financial_calculation（source card = business 卡）；
    - macro：macro_observation card + company document card；
    - valuation：relative_valuation_comparison。

    `research_question` 决定 document 卡提取时绑定的研究问题（Gate C 要求
    document 证据卡与任务研究问题一致才算 ready 输入）。
    """
    biz = await _seed_claim_doc_card(
        env,
        statement="2024年贵州茅台营业收入同比增长15%。",
        source_url=_URL_B,
        research_question=research_question,
    )
    risk = await _seed_claim_doc_card(
        env,
        statement="白酒行业竞争加剧或影响公司毛利率。",
        source_url=_URL_R,
        research_question=research_question,
    )
    macro_card, _ = await _seed_macro_card(env, monkeypatch)
    company_doc = await _seed_macro_doc_card(env, research_question=research_question)
    # financial calc 的 source evidence card = business 卡（env["evidence_card_id"] 约定）。
    env["evidence_card_id"] = biz["evidence_card_id"]
    obs = await _annual_revenue_pair(env)
    calc = await _calc(env, obs)
    comparison = await _seed_comparison(env)
    return {
        "biz_card": biz["evidence_card_id"],
        "risk_card": risk["evidence_card_id"],
        "macro_card": macro_card,
        "company_doc": company_doc,
        "calc": calc.calculation_id,
        "comparison": comparison.comparison_id,
    }


def _request(env: dict, ids: dict) -> Stage4WorkflowRequest:
    return Stage4WorkflowRequest(
        task_id=env["task_id"],
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        analysis_work_items=[
            {"item_id": "biz", "analysis_type": "business", "evidence_card_ids": [ids["biz_card"]]},
            {"item_id": "rsk", "analysis_type": "risk", "evidence_card_ids": [ids["risk_card"]]},
            {
                "item_id": "fin",
                "analysis_type": "financial",
                "calculation_ids": [ids["calc"]],
                "additional_evidence_ids": [],
            },
            {
                "item_id": "mac",
                "analysis_type": "macro",
                "macro_driver_evidence_ids": [ids["macro_card"]],
                "company_evidence_ids": [ids["company_doc"]],
            },
            {
                "item_id": "val",
                "analysis_type": "valuation",
                "comparison_ids": [ids["comparison"]],
            },
        ],
    )


# ---------------------------------------------------------------- fake decisions


def _claim_decision() -> ClaimAnalysisDecision:
    return ClaimAnalysisDecision(
        relevant=True,
        claims=[
            ClaimCandidate(
                statement="公司营收保持增长态势。",
                claim_kind=ClaimKind.INFERENCE,
                confidence=ClaimConfidence.MEDIUM,
                importance=ClaimImportance.NORMAL,
                support_refs=["E1"],
                contradict_refs=[],
                context_refs=[],
            )
        ],
    )


def _financial_decision() -> FinancialAnalysisDecision:
    return FinancialAnalysisDecision(
        relevant=True,
        claims=[
            FinancialClaimCandidate(
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
        ],
    )


def _macro_decision() -> MacroAnalysisDecision:
    return MacroAnalysisDecision(
        relevant=True,
        claims=[
            MacroClaimCandidate(
                statement="利率上行或对公司融资成本形成压力。",
                claim_kind=ClaimKind.RISK,
                confidence=MacroClaimConfidence.MEDIUM,
                importance=MacroClaimImportance.NORMAL,
                channel_type=MacroChannelType.FINANCING,
                effect_direction=MacroEffectDirection.HEADWIND,
                impact_status=MacroImpactStatus.PLAUSIBLE_IMPACT,
                time_alignment=MacroTimeAlignment.ALIGNED,
                macro_driver_refs=["M1"],
                company_exposure_refs=["E1"],
                observed_effect_refs=[],
                additional_support_evidence_refs=[],
                additional_contradict_evidence_refs=[],
                additional_context_evidence_refs=[],
            )
        ],
    )


def _valuation_decision() -> ValuationAnalysisDecision:
    return ValuationAnalysisDecision(
        relevant=True,
        assessment=ValuationClaimAssessment.RELATIVE_HIGH,
        confidence=ValuationClaimConfidence.HIGH,
        importance=ValuationClaimImportance.NORMAL,
        support_comparison_refs=["V1"],
        contradict_comparison_refs=[],
        context_comparison_refs=[],
        reason_code=None,
    )


def _synthesis_output(count: int = 5) -> SynthesisAnalysisOutput:
    refs = [f"C{i + 1}" for i in range(count)]
    return SynthesisAnalysisOutput(
        summary="综合判断：营收增长确定、财务稳健、宏观有传导、估值偏高。",
        themes=[
            SynthesisTheme(
                title="多维度证据支持",
                summary="各 domain 证据指向一致。",
                claim_refs=refs,
            )
        ],
        claim_roles=[
            SynthesisClaimRoleAssignment(
                claim_ref=ref,
                role=SynthesisClaimRole.SUPPORT,
                rationale=f"支持 {ref}",
            )
            for ref in refs
        ],
        duplicates=[],
        conflicts=[],
        evidence_gaps=[
            SynthesisEvidenceGap(
                description="缺少经营现金流证据",
                claim_refs=refs[:1],
                suggested_evidence="经营现金流数据",
                priority=SynthesisPriority.MEDIUM,
            )
        ],
    )


def _build_deps(sessionmaker, models: dict) -> Stage4AnalysisDependencies:
    return Stage4AnalysisDependencies(
        sessionmaker=sessionmaker,
        claim_analysis_service=ClaimAnalysisService(sessionmaker, models["claim"]),
        financial_analysis_service=FinancialAnalysisService(sessionmaker, models["financial"]),
        macro_analysis_service=MacroAnalysisService(sessionmaker, models["macro"]),
        valuation_analysis_service=ValuationAnalysisService(sessionmaker, models["valuation"]),
        synthesis_service=SynthesisService(sessionmaker),
        synthesis_analysis_service=SynthesisAnalysisService(sessionmaker, models["synthesis"]),
    )


def _good_models() -> dict:
    return {
        "claim": FakeClaimAnalysisModel(decision=_claim_decision()),
        "financial": FakeFinancialAnalysisModel(decision=_financial_decision()),
        "macro": FakeMacroAnalysisModel(decision=_macro_decision()),
        "valuation": FakeValuationAnalysisModel(decision=_valuation_decision()),
        "synthesis": FakeSynthesisAnalysisModel(output=_synthesis_output(5)),
    }


async def _claim_count_for_company(sessionmaker, company_id) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM claims WHERE company_id = :cid").bindparams(
                        cid=company_id
                    )
                )
            ).scalar_one()
        )


async def _synthesis_counts(sessionmaker) -> tuple[int, int]:
    async with sessionmaker() as session:
        runs = int(
            (await session.execute(text("SELECT count(*) FROM claim_synthesis_runs"))).scalar_one()
        )
        results = int(
            (
                await session.execute(text("SELECT count(*) FROM claim_synthesis_results"))
            ).scalar_one()
        )
    return runs, results


async def _run_events(sessionmaker, run_id) -> list[dict]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT event_type, node_name, stage, progress, payload "
                    "FROM workflow_events WHERE run_id = :rid ORDER BY created_at"
                ).bindparams(rid=run_id)
            )
        ).mappings()
        return [dict(row) for row in rows]


# ---------------------------------------------------------------- full-chain E2E


async def test_full_chain_e2e(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    models = _good_models()
    deps = _build_deps(env["sessionmaker"], models)

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        result = await runner.execute_stage4(run.run_id, request)
    finally:
        await manager.close()

    assert run.status.value == "pending"
    assert (await runner.get_run(run.run_id)).status.value == "completed"

    # 5 类 worker 各 1 条 Claim → 5 条 unique Claim。
    assert await _claim_count_for_company(env["sessionmaker"], env["company_id"]) == 5
    assert len(result["claim_ids"]) == 5
    assert len(set(result["claim_ids"])) == 5
    assert result["claim_ids"] == sorted(result["claim_ids"])

    # synthesis 恰好一次 → 1 run + 1 result。
    runs, results = await _synthesis_counts(env["sessionmaker"])
    assert (runs, results) == (1, 1)
    assert result["synthesis_id"] is not None
    assert result["synthesis_result_id"] is not None

    # 每个 worker model 恰好调用一次（business/risk 共享 claim model → 2 次）。
    assert len(models["claim"].calls) == 2
    assert len(models["financial"].calls) == 1
    assert len(models["macro"].calls) == 1
    assert len(models["valuation"].calls) == 1
    assert len(models["synthesis"].calls) == 1


async def test_worker_completion_order_does_not_matter_for_fingerprint(
    env, monkeypatch, connection_uri
) -> None:
    """spec Q：5 类 worker 并发完成顺序不改变 claim_ids 与 synthesis 输出。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)

    async def _run_once() -> tuple[str, list[str]]:
        models = _good_models()
        deps = _build_deps(env["sessionmaker"], models)
        manager = LangGraphCheckpointManager(connection_uri)
        await manager.setup()
        try:
            runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
            run = await runner.create_stage4_run(request)
            result = await runner.execute_stage4(run.run_id, request)
        finally:
            await manager.close()
        return result["synthesis_id"], result["claim_ids"]

    first_sid, first_claims = await _run_once()
    second_sid, second_claims = await _run_once()
    # 同输入 → 同 SynthesisRun（fingerprint 幂等）；claim_ids 规范有序。
    assert first_sid == second_sid
    assert first_claims == second_claims == sorted(first_claims)


# ---------------------------------------------------------------- durable resume (spec N-O)


async def test_durable_resume_after_worker_failure(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)

    models_a = _good_models()

    class SlowFailingFinancial(FakeFinancialAnalysisModel):
        """慢失败：先让其余 worker 完成并 checkpoint，再抛 provider 错误。"""

        async def analyze(self, context, calculation_pack, evidence_pack):
            self.calls.append((context, calculation_pack, evidence_pack))
            await asyncio.sleep(0.5)
            raise FinancialAnalysisModelUnavailable()

    fail_financial = SlowFailingFinancial()
    models_a["financial"] = fail_financial
    deps_a = _build_deps(env["sessionmaker"], models_a)

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner_a = Stage4WorkflowRunner(env["sessionmaker"], manager, deps_a)
        run = await runner_a.create_stage4_run(request)
        with pytest.raises(FinancialAnalysisModelUnavailable):
            await runner_a.execute_stage4(run.run_id, request)
        assert (await runner_a.get_run(run.run_id)).status.value == "failed"
        assert len(fail_financial.calls) == 3  # financial worker 有界重试后失败
        # business/risk 已完成（慢失败留足时间）→ claim model 调用 2 次。
        assert len(models_a["claim"].calls) == 2

        # 新 runner B + 同 thread_id（同 run_id）→ 从最后 checkpoint 继续。
        models_b = _good_models()
        deps_b = _build_deps(env["sessionmaker"], models_b)
        runner_b = Stage4WorkflowRunner(env["sessionmaker"], manager, deps_b)
        result = await runner_b.resume_stage4(run.run_id)
        assert (await runner_b.get_run(run.run_id)).status.value == "completed"
        assert len(result["claim_ids"]) == 5
        assert result["synthesis_id"] is not None
        # 已完成 worker 不重跑：B 只重跑失败的 financial worker。
        assert models_b["claim"].calls == []
        assert models_b["macro"].calls == []
        assert models_b["valuation"].calls == []
        assert len(models_b["financial"].calls) == 1
    finally:
        await manager.close()

    # 无重复业务对象：5 类 worker 各 1 条 Claim、1 run、1 result。
    assert await _claim_count_for_company(env["sessionmaker"], env["company_id"]) == 5
    runs, results = await _synthesis_counts(env["sessionmaker"])
    assert (runs, results) == (1, 1)


# ---------------------------------------------------------------- events (spec P)


async def test_events_are_structured_no_prompt_or_evidence_leak(
    env, monkeypatch, connection_uri
) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    deps = _build_deps(env["sessionmaker"], _good_models())

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        await runner.execute_stage4(run.run_id, request)
        run_id = run.run_id
    finally:
        await manager.close()

    events = await _run_events(env["sessionmaker"], run_id)
    types = [e["event_type"] for e in events]
    assert "run_created" in types
    assert "run_started" in types
    assert "node_completed" in types
    assert "run_completed" in types

    node_names = {e["node_name"] for e in events if e["node_name"]}
    assert {
        "validate_analysis_plan",
        "run_analysis_item",
        "collect_claim_ids",
        "synthesize_claims",
    } <= node_names

    # 事件 payload 只允许结构化 keys；不允许 Evidence text / prompt / raw response / reasoning。
    allowed_payload_keys = {
        "graph_name",
        "graph_version",
        "status",
        "item_id",
        "analysis_type",
        "claim_count",
        "synthesis_id",
        "synthesis_result_id",
        "synthesis_complete",
    }
    forbidden_substrings = (
        "evidence_statement",
        "reasoning_content",
        "prompt",
        "raw_response",
        "result_value",
    )
    for event in events:
        payload = event["payload"] or {}
        assert isinstance(payload, dict)
        assert set(payload.keys()) <= allowed_payload_keys, f"leak: {payload}"
        blob = json.dumps(payload, ensure_ascii=False)
        for forbidden in forbidden_substrings:
            assert forbidden not in blob


# ---------------------------------------------------------------- boundary (spec R)


async def test_boundary_no_stage5_tables(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    deps = _build_deps(env["sessionmaker"], _good_models())

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        await runner.execute_stage4(run.run_id, request)
    finally:
        await manager.close()

    async with env["sessionmaker"]() as session:
        # 未来阶段（5E+）表不得存在；Stage 5A-5D 表（report_outlines /
        # draft_sections / reports / report_check_results / report_audits /
        # review_issues，migration 0032-0035）可以存在。
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_name IN "
                "('audits', 'report_sections')"
            )
        )
        assert result.scalars().all() == []
        # Stage 5A 的 report_outlines 表已存在（migration 0032），但 Stage 4 运行
        # 不产生提纲 → 0 行。
        outline_rows = (
            await session.execute(text("SELECT count(*) FROM report_outlines"))
        ).scalar_one()
        assert int(outline_rows) == 0
        # Stage 5B 的 draft_sections 表已存在（migration 0033），但 Stage 4 运行
        # 不产生草稿 → 0 行。
        draft_rows = (
            await session.execute(text("SELECT count(*) FROM draft_sections"))
        ).scalar_one()
        assert int(draft_rows) == 0
        # Stage 5C 的 reports / report_check_results 表已存在（migration 0034），
        # 但 Stage 4 运行不产生报告 → 0 行。
        report_rows = (await session.execute(text("SELECT count(*) FROM reports"))).scalar_one()
        assert int(report_rows) == 0
        check_rows = (
            await session.execute(text("SELECT count(*) FROM report_check_results"))
        ).scalar_one()
        assert int(check_rows) == 0
        # Stage 5D 的 report_audits / review_issues 表已存在（migration 0035），
        # 但 Stage 4 运行不产生审计 → 0 行。
        audit_rows = (
            await session.execute(text("SELECT count(*) FROM report_audits"))
        ).scalar_one()
        assert int(audit_rows) == 0
        issue_rows = (
            await session.execute(text("SELECT count(*) FROM review_issues"))
        ).scalar_one()
        assert int(issue_rows) == 0


# ---------------------------------------------------------------- run state machine


async def test_workflow_run_states_transition(env, monkeypatch, connection_uri) -> None:
    """spec N：run 状态机 pending → running → completed；事件同步。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    deps = _build_deps(env["sessionmaker"], _good_models())

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
        run = await runner.create_stage4_run(request)
        assert run.status.value == "pending"
        await runner.execute_stage4(run.run_id, request)
        assert (await runner.get_run(run.run_id)).status.value == "completed"
        assert (await runner.get_run(run.run_id)).graph_name == "stage4_analysis"
    finally:
        await manager.close()
