"""Top-level research recovery coordinator tests (spec E).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer / AsyncPostgresSaver）+ Fake
LLM models，全程**零真实 DeepSeek**。覆盖三个恢复场景：

1. **崩溃窗口（Q3，spec 必测）**：Stage4 COMPLETED、Stage5 未创建（进程在
   Stage4→Stage5 的内存编排断点死亡）→ 重启后 coordinator 读 checkpoint 直接
   调度 Stage5 → waiting_human → approve → completed；**无重复 Stage4 产物**
   （claims==5、claim_synthesis_runs==1、claim_synthesis_results==1）、恰好
   1 个 stage4 + 1 个 stage5 run；
2. **waiting_human 重启（Q4）**：恢复到 WAITING_HUMAN 后换新 service 实例
   （模拟重启）→ `resume_human(approve)` 原生可恢复 → completed；
3. **worker_restarted Stage4（Q1）**：graph 运行中被取消（run 保持 RUNNING、
   部分 checkpoint）→ 真实 `reconcile_orphaned_runs` 标 FAILED(worker_restarted)
   → coordinator `resume_stage4` 从 checkpoint 续跑 → stage5 → completed，
   无重复产物。

Q2（Stage5 RUNNING 中断恢复）超出最小范围：见 coordinator 模块注释与报告。
"""

import asyncio
import time
from uuid import UUID

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_recovery import ResearchExecutionRecoveryCoordinator
from app.services.research_execution_service import ResearchExecutionService
from app.services.source_registry_service import SourceRegistryService
from app.services.workflow_recovery_service import WorkflowRecoveryService
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
from tests.audit.fakes import FakeAuditModel
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_report_audit_service import human_review_decision
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _build_deps as _stage4_deps,
)
from tests.integration.test_stage4_workflow import (
    _claim_count_for_company,
    _financial_decision,
    _good_models,
    _request,
    _seed_research_task,
    _seed_worker_inputs,
    _synthesis_counts,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- fixtures


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
    await _cleanup_with_revisions(sessionmaker)
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
    await _cleanup_with_revisions(sessionmaker)


# ---------------------------------------------------------------- helpers


def _stage5_factory(sessionmaker, checkpoint):
    return lambda: Stage5WorkflowRunner(
        sessionmaker,
        checkpoint,
        _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=human_review_decision),
            revision_model=FakeRevisionWriterModel(),
        ),
    )


def _make_execution(sessionmaker, checkpoint, *, stage4_deps_factory=None):
    """全新 ResearchExecutionService 实例（模拟进程重启：内存 _chain_state 为空）。"""

    def _default_deps():
        return _stage4_deps(sessionmaker, _good_models())

    if stage4_deps_factory is None:
        stage4_deps_factory = _default_deps
    return ResearchExecutionService(
        sessionmaker=sessionmaker,
        checkpoint_manager=checkpoint,
        company_identity=CompanyIdentityService(sessionmaker),
        stage4_runner_factory=lambda: Stage4WorkflowRunner(
            sessionmaker, checkpoint, stage4_deps_factory()
        ),
        stage5_runner_factory=_stage5_factory(sessionmaker, checkpoint),
    )


async def _run_stage4_to_completed(env, monkeypatch, connection_uri):
    """seed worker inputs → 直接跑完 Stage4 → 返回 (checkpoint_manager, stage4_run_id, request)。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    deps = _stage4_deps(env["sessionmaker"], _good_models())
    runner = Stage4WorkflowRunner(env["sessionmaker"], manager, deps)
    run = await runner.create_stage4_run(request)
    await runner.execute_stage4(run.run_id, request)
    return manager, run.run_id, request


async def _get_run_status(sessionmaker, run_id) -> str:
    async with sessionmaker() as session:
        return (
            await session.execute(
                text("SELECT status FROM workflow_runs WHERE run_id = :rid").bindparams(rid=run_id)
            )
        ).scalar_one()


async def _runs_for_task(sessionmaker, task_id) -> list[dict]:
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


async def _wait_for_stage5_waiting_human(sessionmaker, task_id, timeout: float = 60.0) -> dict:
    """等待后台恢复链创建 Stage5 并到达 WAITING_HUMAN；返回该 stage5 run。"""
    deadline = time.monotonic() + timeout
    while True:
        runs = await _runs_for_task(sessionmaker, task_id)
        stage5 = [r for r in runs if r["graph_name"] == "stage5_report"]
        if stage5 and stage5[0]["status"] == "waiting_human":
            return stage5[0]
        if time.monotonic() > deadline:
            raise AssertionError(f"stage5 未在 {timeout}s 内到达 waiting_human: {runs}")
        await asyncio.sleep(0.25)


async def _assert_no_duplicate_artifacts(sessionmaker, company_id, task_id) -> None:
    """无重复 Stage4 产物 + 恰好一个 stage4 + 一个 stage5 run（无两个 active run）。"""
    assert await _claim_count_for_company(sessionmaker, company_id) == 5
    runs, results = await _synthesis_counts(sessionmaker)
    assert (runs, results) == (1, 1)
    runs = await _runs_for_task(sessionmaker, task_id)
    assert len(runs) == 2
    assert {r["graph_name"] for r in runs} == {"stage4_analysis", "stage5_report"}
    assert all(r["status"] in ("completed", "failed", "cancelled", "waiting_human") for r in runs)


# ---------------------------------------------------------------- Q3 crash window


async def test_crash_window_recovery_stage4_completed(env, monkeypatch, connection_uri) -> None:
    """崩溃窗口：Stage4 完成 → Stage5 未创建 → 重启 coordinator 续接 → 最终完成。"""
    manager, stage4_run_id, request = await _run_stage4_to_completed(
        env, monkeypatch, connection_uri
    )
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    try:
        # 崩溃窗口成立：Stage4 COMPLETED，尚无任何 Stage5 run。
        assert await _get_run_status(sessionmaker, stage4_run_id) == "completed"
        assert [r["graph_name"] for r in await _runs_for_task(sessionmaker, task_id)] == [
            "stage4_analysis"
        ]

        execution = _make_execution(sessionmaker, manager)
        try:
            coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, execution)
            assert await coordinator.recover_interrupted_chains() == 1

            # 后台链续接 → stage5 run → WAITING_HUMAN（人工裁决中断）。
            stage5 = await _wait_for_stage5_waiting_human(sessionmaker, task_id)

            # approve → finalize → completed。
            await execution.resume_human(UUID(stage5["run_id"]), "approve", "审核通过")
            assert await _get_run_status(sessionmaker, UUID(stage5["run_id"])) == "completed"
        finally:
            await execution.close()
    finally:
        await manager.close()

    await _assert_no_duplicate_artifacts(sessionmaker, env["company_id"], task_id)


# ---------------------------------------------------------------- Q4 waiting_human restart


async def test_waiting_human_survives_restart(env, monkeypatch, connection_uri) -> None:
    """WAITING_HUMAN 原生可恢复：换新 service 实例（模拟重启）后 approve 仍完成。"""
    manager, stage4_run_id, request = await _run_stage4_to_completed(
        env, monkeypatch, connection_uri
    )
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    try:
        # 第一次"进程"：coordinator 恢复 → Stage5 WAITING_HUMAN，然后关闭（模拟重启）。
        execution = _make_execution(sessionmaker, manager)
        try:
            coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, execution)
            assert await coordinator.recover_interrupted_chains() == 1
            stage5 = await _wait_for_stage5_waiting_human(sessionmaker, task_id)
        finally:
            await execution.close()
        stage5_run_id = UUID(stage5["run_id"])
        assert await _get_run_status(sessionmaker, stage5_run_id) == "waiting_human"

        # 重启后的新 service：resume_human 从 checkpoint 原生恢复（不依赖旧内存状态）。
        fresh = _make_execution(sessionmaker, manager)
        try:
            await fresh.resume_human(stage5_run_id, "approve", "重启后批准")
        finally:
            await fresh.close()
        assert await _get_run_status(sessionmaker, stage5_run_id) == "completed"
    finally:
        await manager.close()

    await _assert_no_duplicate_artifacts(sessionmaker, env["company_id"], task_id)


# ---------------------------------------------------------------- Q1 worker_restarted stage4


class _GatedFinancial(FakeFinancialAnalysisModel):
    """阻塞在 asyncio.Event 上的 financial worker：用于模拟进程在 graph 运行中死亡。"""

    def __init__(self, gate) -> None:
        super().__init__(decision=_financial_decision())
        self._gate = gate

    async def analyze(
        self,
        context,
        calculation_pack,
        evidence_pack,
        correction_hint: str | None = None,
    ):
        self.calls.append((context, calculation_pack, evidence_pack))
        await self._gate.wait()  # 进程"卡死"在节点内，直到测试取消。
        return await super().analyze(context, calculation_pack, evidence_pack)


async def test_recovery_after_worker_restarted_stage4(env, monkeypatch, connection_uri) -> None:
    """Q1：Stage4 运行中被中断（RUNNING + 部分 checkpoint）→ reconcile 标
    FAILED(worker_restarted) → coordinator resume → 最终完成、无重复产物。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        gate = asyncio.Event()
        models_a = _good_models()
        models_a["financial"] = _GatedFinancial(gate)
        deps_a = _stage4_deps(sessionmaker, models_a)
        runner_a = Stage4WorkflowRunner(sessionmaker, manager, deps_a)
        run = await runner_a.create_stage4_run(request)

        # 模拟进程死亡：后台执行 graph，确认 financial worker 已启动并阻塞，然后取消。
        # 取消 → CancelledError 经 _run_graph 原样抛出 → run 保持 RUNNING + 部分 checkpoint
        # （business/risk/macro/valuation 已完成并持久化，financial 未完成）。
        task = asyncio.create_task(runner_a.execute_stage4(run.run_id, request))
        deadline = time.monotonic() + 15
        while not models_a["financial"].calls and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert models_a["financial"].calls, "financial worker 未启动"
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert await _get_run_status(sessionmaker, run.run_id) == "running"

        # 重启启动路径：真实 reconcile 把 RUNNING → FAILED(worker_restarted)。
        recovery = WorkflowRecoveryService(sessionmaker)
        assert (await recovery.reconcile_orphaned_runs()).marked_failed == 1
        assert await _get_run_status(sessionmaker, run.run_id) == "failed"

        # coordinator（good models factory）→ resume_stage4 → stage5 → waiting_human → approve。
        execution = _make_execution(sessionmaker, manager)
        try:
            coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, execution)
            assert await coordinator.recover_interrupted_chains() == 1
            stage5 = await _wait_for_stage5_waiting_human(sessionmaker, task_id)
            await execution.resume_human(UUID(stage5["run_id"]), "approve", "ok")
            assert await _get_run_status(sessionmaker, UUID(stage5["run_id"])) == "completed"
        finally:
            await execution.close()
    finally:
        await manager.close()

    await _assert_no_duplicate_artifacts(sessionmaker, env["company_id"], task_id)
