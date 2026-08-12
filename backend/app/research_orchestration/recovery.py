"""Top-level research orchestration recovery (stage 7A.2B.1 spec O).

在 `WorkflowRecoveryService.reconcile_orphaned_runs`（把重启时 PENDING/RUNNING
的 WorkflowRun 标为 FAILED(worker_restarted)）**之后**运行：对每个非终态
orchestration，用**同 orchestration_id + 同顶层 thread** 从最后 checkpoint
恢复——**绝不新建 orchestration / 绝不换 thread**（spec O）。

- 候选：`research_orchestration_runs` 里 `status IN (pending, running)` 且
  `current_phase <> 'awaiting_stage5'`（awaiting_stage5 是 7A.2B.1 正常 terminal
  phase，等 7A.2B.2 接 Stage5，不恢复；waiting_human / waiting_manual 也排除——
  等人工，不自动恢复）；
- `phase=stage4` 时先做 **exact child 检查**（spec D：`(orchestration_id, stage4,
  attempt 1)`，**不用 latest task+graph_name 猜归属**）：child run 仍
  `running` → 有 live executor 正在执行（rolling restart）→ **跳过，不重复
  执行**；其余状态（pending / failed(worker_restarted) / completed）→ 交给顶层
  graph 的 `run_or_resume_stage4` 节点（execute / resume / 跳过 collect），
  协调器只恢复顶层 graph，不重复实现 child 恢复；
- 每个 orchestration 独立 try/except：单个失败不中止整个 sweep。
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.domain.tasks import WorkflowRunStatus
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    ChildStage,
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from app.research_orchestration.runner import ResearchOrchestrationRunner

logger = get_logger("app.research_orchestration_recovery")

_TERMINAL_ORCHESTRATION_STATUSES = frozenset(
    {
        OrchestrationStatus.COMPLETED.value,
        OrchestrationStatus.FAILED.value,
        OrchestrationStatus.CANCELLED.value,
    }
)

_ORCHESTRATION_CANDIDATES_SQL = text(
    """
    SELECT orchestration_id::text AS orchestration_id
    FROM research_orchestration_runs
    WHERE status IN ('pending', 'running')
      AND current_phase <> 'awaiting_stage5'
    ORDER BY created_at ASC
    """
)


class ResearchOrchestrationRecoveryCoordinator:
    """Startup coordinator：为中断的 top-level orchestration 从同 thread 恢复。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        runner: ResearchOrchestrationRunner,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runner = runner

    async def recover_orchestrations(self) -> int:
        """扫描候选 → 逐个恢复；返回成功恢复数量（幂等可重跑）。"""
        candidates = await self._find_candidates()
        recovered = 0
        for orchestration_id in candidates:
            try:
                ok = await self._recover_one(orchestration_id)
                recovered += 1 if ok else 0
            except Exception as exc:
                logger.warning(
                    "research_orchestration_recovery_failed",
                    orchestration_id=str(orchestration_id),
                    error_type=type(exc).__name__,
                )
        logger.info(
            "research_orchestrations_recovered",
            candidates=len(candidates),
            recovered=recovered,
        )
        return recovered

    async def _find_candidates(self) -> list[UUID]:
        async with self._sessionmaker() as session:
            result = await session.execute(_ORCHESTRATION_CANDIDATES_SQL)
            rows = result.all()
        return [UUID(str(row[0])) for row in rows]

    async def _recover_one(self, orchestration_id: UUID) -> bool:
        """单个 orchestration：phase 判定 → exact child 检查 → 恢复顶层 graph。"""
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
        if orchestration is None:
            return False
        if orchestration.status in _TERMINAL_ORCHESTRATION_STATUSES:
            return False
        if orchestration.current_phase == OrchestrationPhase.AWAITING_STAGE5.value:
            return False

        checkpoint = await self._runner.read_orchestration_checkpoint(orchestration_id)
        phase = checkpoint.get("current_phase") or orchestration.current_phase
        if phase == OrchestrationPhase.STAGE4.value and not await self._child_resumable(
            orchestration_id
        ):
            return False

        await self._runner.run_orchestration(orchestration_id)
        return True

    async def _child_resumable(self, orchestration_id: UUID) -> bool:
        """exact child (orchestration_id, stage4, attempt 1) 是否可恢复。

        - 无 child（crash 在 ensure_stage4_child 完成前）→ True（graph 重新
          ensure / execute）；
        - child `running` → False（live executor / rolling restart，不重复执行）；
        - pending / failed(worker_restarted) / completed → True（graph 节点
          execute / resume / collect）。
        """
        async with self._sessionmaker() as session:
            child = await ResearchOrchestrationChildRepository(session).get_child(
                orchestration_id, ChildStage.STAGE4.value, 1
            )
            if child is None:
                return True
            run = await WorkflowRunRepository(session).get_by_id(child.workflow_run_id)
            if run is None:
                logger.warning(
                    "research_orchestration_child_run_missing",
                    orchestration_id=str(orchestration_id),
                    workflow_run_id=str(child.workflow_run_id),
                )
                return False
            if run.status == WorkflowRunStatus.RUNNING.value:
                logger.info(
                    "research_orchestration_child_running_skip",
                    orchestration_id=str(orchestration_id),
                    workflow_run_id=str(run.run_id),
                )
                return False
            return True
