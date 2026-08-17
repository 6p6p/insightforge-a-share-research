"""Top-level research orchestration integration tests (stage 7A.2B.1 spec S).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer / AsyncPostgresSaver）+ 真实
`research_orchestration_runs` / `research_orchestration_child_runs` /
`workflow_runs` 表 + 真实 Stage4 runner + 真实 Synthesis；plan/router 真实
（FakeResearchPlannerModel），prepare/fulfill 注入可控 Fake（readiness 由测试
控制，不重测 7A.1 / 7A.2A 语义）。全程**零真实 DeepSeek**。

Concentrated Cases 1-6（spec S；7A.2B.2 接入 Stage5 后顶层 graph 走到 completed）：
1. happy path：prepare ready → ensure_stage4_child → 真实 Stage4 → collect_synthesis
   → Stage5 → **completed**（status=completed、phase=completed；恰好 1 stage4 + 1
   stage5 child + exact child links + 1 份 synthesis，无重复产物）；
2. not ready → fulfill → prepare_again ready → Stage4 → Stage5 → completed；
3. fulfill 后仍 not ready → **waiting_manual**（status=waiting_human，0 个
   workflow_run / 0 个 child link）；
4. Stage4 child 业务失败 → orchestration **failed**（phase=stage4，
   error_code=stage4_execution_failed；child run failed）；
5. user retry（spec P）：同输入 replay 同一 orchestration（replayed=True）；改 task
   研究问题（新 fingerprint）+ task 已有 active orchestration →
   `ResearchOrchestrationActiveConflict`（409）；
6. worker restart（spec O）：顶层 graph 运行中取消（child RUNNING + 部分
   checkpoint）→ 真实 `reconcile_orphaned_runs` 标 FAILED(worker_restarted) →
   `ResearchOrchestrationRecoveryCoordinator` **同 orchestration_id + 同顶层
   thread** 恢复 → 完整链 Stage5 → completed，无重复产物。
"""

import asyncio
import time
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.analysis.financial.errors import FinancialAnalysisModelUnavailable
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.errors import ResearchOrchestrationActiveConflict
from app.research_orchestration.recovery import ResearchOrchestrationRecoveryCoordinator
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import (
    ResearchOrchestrationChildService,
    ResearchOrchestrationService,
)
from app.research_planning.preparation import (
    MissingReasonCode,
    MissingResearchNeed,
    ResearchPreparationResult,
)
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.services.workflow_recovery_service import WorkflowRecoveryService
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_research_planning_service import (
    _plan_payload,
    _seed_research_task,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _build_deps as _stage4_deps,
)
from tests.integration.test_stage4_workflow import (
    _claim_count_for_company,
    _financial_decision,
    _good_models,
    _request,
    _seed_worker_inputs,
    _synthesis_counts,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_valuation_claim_service import _seed_company
from tests.research_planning.fakes import FakeResearchPlannerModel
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    """先删 orchestration / plan 层（FK RESTRICT 引用 workflow_runs /
    research_plans / research_tasks），再走公共 `_cleanup_with_revisions`。
    基类 cleanup 不删 research_plans——orchestration 必然 create_plan，
    必须先删 plans（及 routes），否则基类删 research_tasks 会被 FK 拒绝。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM research_orchestration_child_runs"))
        await session.execute(text("DELETE FROM research_orchestration_runs"))
        await session.execute(text("DELETE FROM research_plan_routes"))
        await session.execute(text("DELETE FROM research_plans"))
        await session.commit()
    await _cleanup_with_revisions(sessionmaker)


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
async def env(tmp_path, sessionmaker) -> dict:
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


# ---------------------------------------------------------------- fake services


class _FakePreparation:
    """可控 readiness 的 prepare：按调用次序返回结果（最后一个结果重复）。"""

    def __init__(self, outcomes) -> None:
        self._outcomes = outcomes
        self.calls = 0

    async def prepare_research(self, research_plan_id: UUID) -> ResearchPreparationResult:
        idx = min(self.calls, len(self._outcomes) - 1)
        self.calls += 1
        ready, request, missing_codes = self._outcomes[idx]
        return ResearchPreparationResult(
            research_plan_id=research_plan_id,
            resolved=(),
            module_inputs=(),
            missing_needs=tuple(
                MissingResearchNeed(code, "document", MissingReasonCode.NOT_FOUND, "fake missing")
                for code in missing_codes
            ),
            ready_for_analysis=ready,
            stage4_request=request,
        )


class _FakeFulfillment:
    """记录调用的 fulfill（readiness 由 fake preparation 控制）。"""

    def __init__(self) -> None:
        self.calls = 0

    async def fulfill_research_needs(self, research_plan_id: UUID):
        self.calls += 1


def _planner(sessionmaker) -> ResearchPlanningService:
    return ResearchPlanningService(
        sessionmaker,
        FakeResearchPlannerModel(payload=_plan_payload()),
        CompanyIdentityService(sessionmaker),
    )


def _orchestration_deps(
    sessionmaker,
    manager,
    request,
    *,
    prep_outcomes=None,
    models=None,
) -> ResearchOrchestrationDependencies:
    """装配 orchestration deps：真实 plan/router/stage4/synthesis + fake prepare/fulfill。"""
    prep_outcomes = prep_outcomes or [(True, request, [])]
    plan_service = _planner(sessionmaker)
    router = ResearchSourceRouter(sessionmaker, plan_service)
    stage4_deps = _stage4_deps(sessionmaker, models if models is not None else _good_models())
    stage4_runner = Stage4WorkflowRunner(sessionmaker, manager, stage4_deps)
    # 7A.2B.2 起 deps 必填 stage5_runner；7A.2B.1 cases 只到 awaiting_stage5，
    # runner 仅装配不执行（fake models，0 LLM）。
    stage5_runner = Stage5WorkflowRunner(
        sessionmaker,
        manager,
        _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        ),
    )
    child_service = ResearchOrchestrationChildService(
        sessionmaker, stage4_runner, stage5_runner=stage5_runner
    )
    return ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=plan_service,
        router=router,
        preparation=_FakePreparation(prep_outcomes),
        fulfillment=_FakeFulfillment(),
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
    )


async def _create_orchestration(sessionmaker, task_id: UUID) -> UUID:
    plan_service = _planner(sessionmaker)
    service = ResearchOrchestrationService(sessionmaker, plan_service)
    result = await service.create_or_get_orchestration(task_id)
    assert result.replayed is False
    return result.orchestration_id


# ---------------------------------------------------------------- read helpers


async def _get_orchestration_row(sessionmaker, orchestration_id: UUID) -> dict:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT status, current_phase, error_code "
                        "FROM research_orchestration_runs WHERE orchestration_id = :oid"
                    ).bindparams(oid=orchestration_id)
                )
            )
            .mappings()
            .one()
        )
        return dict(row)


async def _get_child(sessionmaker, orchestration_id: UUID) -> dict | None:
    async with sessionmaker() as session:
        row = (
            (
                await session.execute(
                    text(
                        "SELECT workflow_run_id::text AS run_id, stage, attempt_no "
                        "FROM research_orchestration_child_runs "
                        "WHERE orchestration_id = :oid AND stage = 'stage4' AND attempt_no = 1"
                    ).bindparams(oid=orchestration_id)
                )
            )
            .mappings()
            .first()
        )
        return dict(row) if row else None


async def _runs_for_task(sessionmaker, task_id: UUID) -> list[dict]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT run_id::text AS run_id, graph_name, status, error_code "
                    "FROM workflow_runs WHERE task_id = :tid ORDER BY created_at, run_id"
                ).bindparams(tid=task_id)
            )
        ).mappings()
        return [dict(r) for r in rows]


async def _count_orchestrations(sessionmaker) -> int:
    async with sessionmaker() as session:
        result = await session.execute(text("SELECT count(*) FROM research_orchestration_runs"))
        return int(result.scalar_one())


# ---------------------------------------------------------------- Case 1 happy path


async def test_case1_happy_path_to_awaiting_stage5(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(sessionmaker, manager, request)
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        assert final["current_phase"] == "completed"
        assert final["synthesis_result_id"]
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        assert row["error_code"] is None

        # 恰好 1 stage4 + 1 stage5 child run（全部 completed），stage4 exact link 在。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert {r["graph_name"] for r in runs} == {"stage4_analysis", "stage5_report"}
        assert all(r["status"] == "completed" for r in runs)
        child = await _get_child(sessionmaker, orchestration_id)
        assert child is not None
        assert child["stage"] == "stage4" and child["attempt_no"] == 1
        stage4_run = next(r for r in runs if r["graph_name"] == "stage4_analysis")
        assert child["run_id"] == stage4_run["run_id"]

        # 真实 Stage4 产物：5 claims + 1 synthesis run + 1 synthesis result。
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (1, 1)
        assert deps.fulfillment.calls == 0
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 2 not ready → fulfill


async def test_case2_not_ready_then_fulfill_then_ready(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            request,
            prep_outcomes=[(False, None, ["news_docs"]), (True, request, [])],
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        assert final["current_phase"] == "completed"
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["current_phase"] == "completed"
        assert deps.fulfillment.calls == 1
        # prepare → fulfill → prepare_again → ensure_stage4_child → run_or_resume。
        assert deps.preparation.calls == 4
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 3 waiting_manual


async def test_case3_fulfill_still_not_ready_waiting_manual(env, connection_uri) -> None:
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        deps = _orchestration_deps(
            sessionmaker,
            manager,
            None,
            prep_outcomes=[(False, None, ["news_docs"]), (False, None, ["news_docs"])],
        )
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        final = await runner.run_orchestration(orchestration_id)

        assert final["current_phase"] == "waiting_manual"
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "waiting_human"
        assert row["current_phase"] == "waiting_manual"
        # 0 个 workflow_run / 0 个 child link（waiting_manual 不创建 Stage4 run）。
        assert await _runs_for_task(sessionmaker, task_id) == []
        assert await _get_child(sessionmaker, orchestration_id) is None
        assert deps.fulfillment.calls == 1
        assert deps.preparation.calls == 2
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 4 stage4 child failure


async def test_case4_stage4_child_failure_projection(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)
        models = _good_models()

        class _SlowFailingFinancial(FakeFinancialAnalysisModel):
            """慢失败：让其余 worker 完成并 checkpoint，再抛 provider 错误。"""

            async def analyze(
                self,
                context,
                calculation_pack,
                evidence_pack,
                correction_hint: str | None = None,
            ):
                self.calls.append((context, calculation_pack, evidence_pack))
                await asyncio.sleep(0.5)
                raise FinancialAnalysisModelUnavailable()

        models["financial"] = _SlowFailingFinancial()
        deps = _orchestration_deps(sessionmaker, manager, request, models=models)
        runner = ResearchOrchestrationRunner(sessionmaker, manager, deps)
        with pytest.raises(FinancialAnalysisModelUnavailable):
            await runner.run_orchestration(orchestration_id)

        # 失败投影（spec M）：phase 保持 stage4、稳定 error_code、不吞 child 错误。
        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "failed"
        assert row["current_phase"] == "stage4"
        assert row["error_code"] == "stage4_execution_failed"
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 1
        assert runs[0]["status"] == "failed"
        assert runs[0]["error_code"] == "workflow_execution_failed"
    finally:
        await manager.close()


# ---------------------------------------------------------------- Case 5 replay + 409


async def test_case5_replay_and_active_conflict(env, connection_uri) -> None:
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    plan_service = _planner(sessionmaker)
    service = ResearchOrchestrationService(sessionmaker, plan_service)

    result1 = await service.create_or_get_orchestration(task_id)
    result2 = await service.create_or_get_orchestration(task_id)
    assert result2.replayed is True
    assert result2.orchestration_id == result1.orchestration_id
    assert await _count_orchestrations(sessionmaker) == 1

    # 新 fingerprint（改 task 研究问题 → 新 plan input → 新 orchestration
    # fingerprint）+ task 已有 active orchestration → 409。
    async with sessionmaker() as session:
        task = await ResearchTaskRepository(session).get_by_id(task_id)
        task.questions = ["新的研究问题？"]
        await session.commit()
    with pytest.raises(ResearchOrchestrationActiveConflict):
        await service.create_or_get_orchestration(task_id)


# ---------------------------------------------------------------- Case 6 recovery same thread


async def test_case6_recovery_same_orchestration_thread(env, monkeypatch, connection_uri) -> None:
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    company_id = env["company_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        orchestration_id = await _create_orchestration(sessionmaker, task_id)

        # 进程 A：gated financial worker → 顶层 graph 运行中取消（模拟进程死亡）。
        gate = asyncio.Event()
        models_a = _good_models()

        class _GatedFinancial(FakeFinancialAnalysisModel):
            def __init__(self, g) -> None:
                super().__init__(decision=_financial_decision())
                self._g = g

            async def analyze(
                self,
                context,
                calculation_pack,
                evidence_pack,
                correction_hint: str | None = None,
            ):
                self.calls.append((context, calculation_pack, evidence_pack))
                await self._g.wait()
                return await super().analyze(context, calculation_pack, evidence_pack)

        models_a["financial"] = _GatedFinancial(gate)
        deps_a = _orchestration_deps(sessionmaker, manager, request, models=models_a)
        runner_a = ResearchOrchestrationRunner(sessionmaker, manager, deps_a)
        task = asyncio.create_task(runner_a.run_orchestration(orchestration_id))
        deadline = time.monotonic() + 15
        while not models_a["financial"].calls and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert models_a["financial"].calls, "financial worker 未启动"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # child run 保持 RUNNING（CancelledError 不标 failed）；顶层 checkpoint
        # 已是 stage4（ensure_stage4_child 已 checkpoint）。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 1
        assert runs[0]["status"] == "running"
        child = await _get_child(sessionmaker, orchestration_id)
        assert child is not None and child["run_id"] == runs[0]["run_id"]

        # 重启启动路径：真实 reconcile → child RUNNING → FAILED(worker_restarted)。
        recovery = WorkflowRecoveryService(sessionmaker)
        assert (await recovery.reconcile_orphaned_runs()).marked_failed == 1
        assert (await _runs_for_task(sessionmaker, task_id))[0]["status"] == "failed"

        # 进程 B：新 runner + 同 orchestration_id → coordinator 恢复（同顶层 thread）。
        deps_b = _orchestration_deps(sessionmaker, manager, request)
        runner_b = ResearchOrchestrationRunner(sessionmaker, manager, deps_b)
        coordinator = ResearchOrchestrationRecoveryCoordinator(sessionmaker, runner_b)
        assert await coordinator.recover_orchestrations() == 1

        row = await _get_orchestration_row(sessionmaker, orchestration_id)
        assert row["status"] == "completed"
        assert row["current_phase"] == "completed"
        assert row["error_code"] is None

        # 无重复产物：仍 1 stage4 + 1 stage5 run（全部 completed）、1 份 synthesis、5 claims。
        runs = await _runs_for_task(sessionmaker, task_id)
        assert len(runs) == 2
        assert {r["graph_name"] for r in runs} == {"stage4_analysis", "stage5_report"}
        assert all(r["status"] == "completed" for r in runs)
        assert await _claim_count_for_company(sessionmaker, company_id) == 5
        s_runs, s_results = await _synthesis_counts(sessionmaker)
        assert (s_runs, s_results) == (1, 1)
    finally:
        await manager.close()
