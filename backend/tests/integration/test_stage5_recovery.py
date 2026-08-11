"""Stage6A final durability：active Stage5 recovery tests (spec A1-A3).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer / AsyncPostgresSaver）+ Fake
LLM models，全程**零真实 DeepSeek**。覆盖：

1. **worker 重启恢复（concentrated，spec A3）**：Stage5 graph 在 audit_report
   节点内被中断（RUNNING + draft/assemble/check 已完成并持久化、audit 未开始）
   → 真实 `reconcile_orphaned_runs` 标 FAILED(worker_restarted) → 新 service
   实例 + coordinator → `resume_stage5_for_recovery` **同 run/thread** 从最后
   checkpoint 恢复 → waiting_human → approve → completed；**无重复**
   DraftSection / Report / Check / Audit / Revision（与中断时快照逐表相等）；
2. **业务失败不自动恢复**：模型抛错 → run FAILED(workflow_execution_failed) →
   `resume_stage5_for_recovery` 拒绝（WorkflowRunAlreadyFinished），coordinator
   不调度；
3. **WAITING_HUMAN 永不自动恢复**：人工中断态重启后 coordinator 调度数为 0
   （等 Web 人工 action，走 `resume_stage5_human`）。
"""

import asyncio
import time
from uuid import UUID

import pytest
import pytest_asyncio

from app.audit.contracts import AuditDecision
from app.audit.packs import AuditPack
from app.core.config import get_settings
from app.core.errors import WorkflowRunAlreadyFinished
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.services.research_execution_recovery import ResearchExecutionRecoveryCoordinator
from app.services.workflow_recovery_service import (
    WORKER_RESTARTED_ERROR_CODE,
    WorkflowRecoveryService,
)
from app.stage5.contracts import Stage5WorkflowRequest
from app.stage5.runner import Stage5WorkflowRunner
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_draft_section_service import (
    _AS_OF,
    _QUESTION,
    _good_models,
    _run_stage4_to_result,
)
from tests.integration.test_report_audit_service import human_review_decision
from tests.integration.test_research_execution_recovery import (
    _assert_no_duplicate_artifacts,
    _get_run_status,
    _make_execution,
    _wait_for_stage5_waiting_human,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage5_workflow import _run_count, _run_row, _stage5_deps
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
    from app.services.source_registry_service import SourceRegistryService
    from app.storage.raw_store import LocalRawArtifactStore
    from tests.integration.test_stage4_workflow import _seed_research_task

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


class _GatedAudit(FakeAuditModel):
    """阻塞在 asyncio.Event 上的 auditor：模拟进程在 audit_report 节点内死亡。

    继承 FakeAuditModel（确定性输出 + calls 记录），在 audit 开始时阻塞，直到
    测试取消后台执行任务。
    """

    def __init__(self, gate: asyncio.Event) -> None:
        super().__init__(decision_factory=human_review_decision)
        self._gate = gate

    async def audit(self, pack: AuditPack) -> AuditDecision:
        self.calls.append(pack)
        await self._gate.wait()  # 进程"卡死"在节点内，直到测试取消。
        if self._decision_factory is not None:
            return self._decision_factory(pack)
        return self._decision


async def _seed_synthesis(env, monkeypatch, connection_uri) -> UUID:
    """完整 Stage4 graph → synthesis_result_id（Fake analysis models）。"""
    return await _run_stage4_to_result(env, monkeypatch, connection_uri, _good_models())


def _request(env, synthesis_result_id: UUID) -> Stage5WorkflowRequest:
    return Stage5WorkflowRequest(
        task_id=env["task_id"],
        company_id=env["company_id"],
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        synthesis_result_id=synthesis_result_id,
    )


# ---------------------------------------------------------------- Q5 worker restart (spec A3)


async def test_stage5_worker_restart_recovery_no_duplicates(
    env, monkeypatch, connection_uri
) -> None:
    """Stage5 mid-execution → reconcile → FAILED(worker_restarted) → 同 run/thread
    恢复 → waiting_human → completed；无重复 DraftSection/Report/Check/Audit/Revision。"""
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        gate = asyncio.Event()
        gated_audit = _GatedAudit(gate)
        deps = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=gated_audit,
            revision_model=FakeRevisionWriterModel(),
        )
        runner = Stage5WorkflowRunner(sessionmaker, manager, deps)
        run = await runner.create_stage5_run(request)

        # 模拟进程死亡：后台执行 graph，audit_report 节点内阻塞后取消。取消 →
        # CancelledError 原样抛出 → run 保持 RUNNING + 部分 checkpoint
        # （build/assemble/check 已完成并持久化，audit 未开始）。
        exec_task = asyncio.create_task(runner.execute_stage5(run.run_id, request))
        deadline = time.monotonic() + 30
        while not gated_audit.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        assert gated_audit.calls, "audit model 未启动"
        exec_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await exec_task
        assert await _get_run_status(sessionmaker, run.run_id) == "running"

        # 中断时已持久化产物快照（恢复后不得重复创建）。
        draft_before = await _run_count(sessionmaker, "draft_sections")
        report_before = await _run_count(sessionmaker, "reports")
        check_before = await _run_count(sessionmaker, "report_check_results")
        assert draft_before > 0 and report_before == 1 and check_before == 1

        # 重启路径：真实 reconcile 把 RUNNING → FAILED(worker_restarted)。
        recovery = WorkflowRecoveryService(sessionmaker)
        assert (await recovery.reconcile_orphaned_runs()).marked_failed == 1
        assert await _get_run_status(sessionmaker, run.run_id) == "failed"

        # 新 service 实例（内存 _chain_state 为空）+ coordinator → Stage5 恢复。
        execution = _make_execution(sessionmaker, manager)
        try:
            assert (
                await ResearchExecutionRecoveryCoordinator(
                    sessionmaker, execution
                ).recover_interrupted_chains()
                == 1
            )
            stage5 = await _wait_for_stage5_waiting_human(sessionmaker, task_id)
            # 关键：同 run/thread 恢复，不是新 WorkflowRun。
            assert stage5["run_id"] == str(run.run_id)
            assert await _get_run_status(sessionmaker, run.run_id) == "waiting_human"

            # 人工 approve → finalize → completed。
            await execution.resume_human(UUID(stage5["run_id"]), "approve", "重启恢复后批准")
            assert await _get_run_status(sessionmaker, run.run_id) == "completed"
        finally:
            await execution.close()
    finally:
        await manager.close()

    # 无重复产物：audit 恰好 1 条、0 revisions；draft/report/check 与中断时快照一致。
    assert await _run_count(sessionmaker, "report_audits") == 1
    assert await _run_count(sessionmaker, "draft_section_revisions") == 0
    assert await _run_count(sessionmaker, "draft_sections") == draft_before
    assert await _run_count(sessionmaker, "reports") == report_before
    assert await _run_count(sessionmaker, "report_check_results") == check_before
    # Stage4 级产物不重复 + 恰好 1 stage4 + 1 stage5 run（无两个 active run）。
    await _assert_no_duplicate_artifacts(sessionmaker, env["company_id"], task_id)


# ---------------------------------------------------------------- business failure（spec A1）


async def test_stage5_business_failure_not_auto_recovered(env, monkeypatch, connection_uri) -> None:
    """业务失败（模型抛错）→ run FAILED(非 worker_restarted) → 不自动恢复。"""
    sessionmaker = env["sessionmaker"]
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        deps = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(
                decision_factory=valid_decision_for, error=RuntimeError
            ),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        runner = Stage5WorkflowRunner(sessionmaker, manager, deps)
        run = await runner.create_stage5_run(request)
        with pytest.raises(RuntimeError):
            await runner.execute_stage5(run.run_id, request)
        row = await _run_row(sessionmaker, run.run_id)
        assert row["status"] == "failed"
        assert row["error_code"] != WORKER_RESTARTED_ERROR_CODE

        # 直接恢复拒绝：业务失败不在 Stage5 recovery 路径。
        with pytest.raises(WorkflowRunAlreadyFinished):
            await runner.resume_stage5_for_recovery(run.run_id)

        # coordinator 不调度业务失败（Stage4 候选被已有 Stage5 排除，Stage5 候选
        # 被 error_code != worker_restarted 排除）。
        execution = _make_execution(sessionmaker, manager)
        try:
            assert (
                await ResearchExecutionRecoveryCoordinator(
                    sessionmaker, execution
                ).recover_interrupted_chains()
                == 0
            )
        finally:
            await execution.close()
    finally:
        await manager.close()


# ---------------------------------------------------------------- waiting_human（spec A2）


async def test_stage5_waiting_human_not_auto_resumed(env, monkeypatch, connection_uri) -> None:
    """WAITING_HUMAN 重启后 coordinator 不自动恢复（等 Web 人工 action）。"""
    sessionmaker = env["sessionmaker"]
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        deps = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=human_review_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        runner = Stage5WorkflowRunner(sessionmaker, manager, deps)
        run = await runner.create_stage5_run(request)
        await runner.execute_stage5(run.run_id, request)
        assert await _get_run_status(sessionmaker, run.run_id) == "waiting_human"

        # 重启后的 coordinator：waiting_human 不是 failed 候选 → 0 调度。
        execution = _make_execution(sessionmaker, manager)
        try:
            assert (
                await ResearchExecutionRecoveryCoordinator(
                    sessionmaker, execution
                ).recover_interrupted_chains()
                == 0
            )
            # 人工 resume 仍可用（Web action 路径）。
            await execution.resume_human(run.run_id, "approve", "人工通过")
        finally:
            await execution.close()
        assert await _get_run_status(sessionmaker, run.run_id) == "completed"
    finally:
        await manager.close()
