"""In-process background scheduler for top-level research orchestrations (7A.2B.2 Gate B).

只负责后台调度顶层 run / resume（参考 `WorkflowExecutionManager` 的 asyncio
registry 模式，**不复制 Stage1 simulation / resume/cancel 的 WorkflowRun 逻辑**——
child / action 语义仍由 `ResearchOrchestrationService` + Stage4/5 runners 承担）。

- `schedule(orchestration_id)`：后台调度 `run_orchestration`；`schedule_resume(
  orchestration_id, kind)`：后台调度 `resume_after_source_acquisition`（7A
  Product Gate spec J：受控补资料后同线程恢复，K1/K2）。两者共享同一 registry
  （同 orchestration_id 至多一个 live task，run 与 resume 互斥）；
- 同 orchestration_id 已调度未完成 → no-op False；task done → 自动从 registry 清除；
- `is_scheduled(orchestration_id)`：查询本地是否已有 live task；
- `cancel_local(orchestration_id)`：协作式取消本地 task（await 完成）。**只负责
  local asyncio task**——orchestration DB status / child cancellation 由
  `ResearchOrchestrationService.cancel_orchestration` 负责（spec F：不出现
  "只 cancel asyncio.Task 但 DB 仍 running"）；
- **异常**：Runner 负责 PG status/error projection（`_mark_orchestration_failed`）；
  本 manager 只安全日志 orchestration_id + error type，**不得输出** prompt /
  Evidence 正文 / key / reasoning / stack。

本 manager **不是 durable truth**：重启恢复仍由
`ResearchOrchestrationRecoveryCoordinator`（读 DB + checkpoint）负责（spec G）。
"""

import asyncio
from uuid import UUID

from app.core.logging import get_logger
from app.research_orchestration.runner import ResearchOrchestrationRunner

logger = get_logger("app.research_orchestration_execution")


class ResearchOrchestrationExecutionManager:
    """Single-process dev scheduler; not a distributed task queue."""

    def __init__(self, runner: ResearchOrchestrationRunner) -> None:
        self._runner = runner
        self._tasks: dict[UUID, asyncio.Task] = {}

    def schedule(self, orchestration_id: UUID) -> bool:
        """后台调度一次顶层 run；同 id 已有 live task → False（不重复）。"""
        return self._schedule(orchestration_id, kind=None)

    def schedule_resume(self, orchestration_id: UUID, kind: str) -> bool:
        """后台调度**受控补资料后同线程恢复**（7A Product Gate spec J）。

        与 `schedule` 共享同一 registry（同 orchestration_id 至多一个 live
        task，run 与 resume 互斥）；kind 传给 runner
        `resume_after_source_acquisition`（K1 prepare / K2 supplemental_research）。
        """
        return self._schedule(orchestration_id, kind=kind)

    def _schedule(self, orchestration_id: UUID, *, kind: str | None) -> bool:
        task = self._tasks.get(orchestration_id)
        if task is not None and not task.done():
            return False
        new_task = asyncio.create_task(
            self._run(orchestration_id, kind=kind), name=f"orchestration-{orchestration_id}"
        )
        self._tasks[orchestration_id] = new_task
        new_task.add_done_callback(self._on_task_done)
        return True

    def is_scheduled(self, orchestration_id: UUID) -> bool:
        task = self._tasks.get(orchestration_id)
        return task is not None and not task.done()

    async def cancel_local(self, orchestration_id: UUID) -> None:
        """协作式取消本地 task（await 完成）；无 live task → no-op。"""
        task = self._tasks.get(orchestration_id)
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            # runner 可能已在 DB 投影终态（竞态）；终态由 DB 原子更新裁决。
            pass

    async def close(self) -> None:
        pending = [task for task in self._tasks.values() if not task.done()]
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()

    # ------------------------------------------------------------------ internal

    async def _run(self, orchestration_id: UUID, *, kind: str | None) -> None:
        """执行顶层 run / resume；异常由 Runner 投影 PG 终态，本层只安全日志。"""
        try:
            if kind is None:
                await self._runner.run_orchestration(orchestration_id)
            else:
                await self._runner.resume_after_source_acquisition(orchestration_id, kind)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "research_orchestration_task_failed",
                orchestration_id=str(orchestration_id),
                error_type=type(exc).__name__,
                error=str(exc)[:400],
                exc_info=True,
            )

    def _on_task_done(self, task: asyncio.Task) -> None:
        name = task.get_name()
        orchestration_id = (
            UUID(name.removeprefix("orchestration-")) if name.startswith("orchestration-") else None
        )
        if orchestration_id is not None:
            self._tasks.pop(orchestration_id, None)
        try:
            task.exception()  # 消费异常，避免 "Task exception was never retrieved"
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "research_orchestration_task_failed",
                orchestration_id=str(orchestration_id),
                error_type=type(exc).__name__,
            )
