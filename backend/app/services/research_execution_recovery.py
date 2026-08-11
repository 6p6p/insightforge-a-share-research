"""Startup recovery for interrupted research chains (spec E).

Stage 4 runs 是 durable 的（PG Checkpointer / AsyncPostgresSaver）；进程重启
只丢失 `ResearchExecutionService._chain_state` 里的内存编排断点——"Stage 4 完成
后创建 Stage 5"。`WorkflowRecoveryService` 已把重启时 PENDING/RUNNING 的 run
标为 FAILED(worker_restarted)；本协调器在 reconcile **之后**重建 Stage4→Stage5
链路：

- Stage4 COMPLETED、且该研究周期尚无 Stage5 run → 读 checkpoint 的
  synthesis_result_id，直接调度 Stage 5；
- Stage4 FAILED(worker_restarted)、且无 Stage5 → 先 `resume_stage4`（同
  run/thread 从最后 checkpoint 继续；synthesis 幂等 → 无重复产物），再调度
  Stage 5。

候选识别按 **task 最近一条 Stage4 run** 锚定当前研究周期（区别于"按公司随便取
最新"）：若该 run 之后已创建过 Stage5（created_at >= run.created_at），说明该
周期已被（或正在被）正常编排，不重复恢复。跨周期场景（周期 1 完成 → 用户再次
execute → 周期 2 Stage4 崩溃）因此安全：周期 1 的 Stage5 早于周期 2 的 Stage4
创建，不影响候选判断。

不做 Q2（Stage5 RUNNING 中断恢复）——超出最小范围，见代码注释与报告。

无消息队列：复用 WorkflowRun 状态机 + PG Checkpointer + 既有 runner。
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.services.research_execution_service import ResearchExecutionService
from app.services.workflow_recovery_service import WORKER_RESTARTED_ERROR_CODE
from app.stage4.graph import STAGE4_GRAPH_NAME
from app.stage5.contracts import STAGE5_GRAPH_NAME

logger = get_logger("app.research_recovery")

_CANDIDATES_SQL = text(
    """
    SELECT DISTINCT ON (r4.task_id)
           r4.task_id::text          AS task_id,
           r4.run_id::text           AS stage4_run_id,
           (r4.status = 'failed')    AS resume_stage4
    FROM workflow_runs r4
    WHERE r4.graph_name = :stage4_graph
      AND (
            r4.status = 'completed'
            OR (r4.status = 'failed' AND r4.error_code = :error_code)
      )
      AND NOT EXISTS (
            SELECT 1 FROM workflow_runs r5
            WHERE r5.task_id = r4.task_id
              AND r5.graph_name = :stage5_graph
              AND r5.created_at >= r4.created_at
      )
    ORDER BY r4.task_id, r4.created_at DESC
    """
)


class ResearchExecutionRecoveryCoordinator:
    """Best-effort startup coordinator：为已中断研究链重新调度 Stage 5 续接。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        research_execution: ResearchExecutionService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._research_execution = research_execution

    async def recover_interrupted_chains(self) -> int:
        """扫描候选 → 逐个调度 Stage 5 续接；返回成功调度数量（幂等可重跑）。"""
        candidates = await self._find_candidates()
        scheduled = 0
        for task_id, stage4_run_id, resume_stage4 in candidates:
            ok = self._research_execution.schedule_recovery(
                task_id=UUID(task_id),
                stage4_run_id=UUID(stage4_run_id),
                resume_stage4=resume_stage4,
            )
            scheduled += 1 if ok else 0
        logger.info(
            "research_chains_recovered",
            candidates=len(candidates),
            scheduled=scheduled,
        )
        return scheduled

    async def _find_candidates(self) -> list[tuple[str, str, bool]]:
        """每个 task 取最近一条 Stage4 run：COMPLETED 或 FAILED(worker_restarted)。"""
        async with self._sessionmaker() as session:
            result = await session.execute(
                _CANDIDATES_SQL,
                {
                    "stage4_graph": STAGE4_GRAPH_NAME,
                    "stage5_graph": STAGE5_GRAPH_NAME,
                    "error_code": WORKER_RESTARTED_ERROR_CODE,
                },
            )
            rows = result.all()
        return [(str(r[0]), str(r[1]), bool(r[2])) for r in rows]
