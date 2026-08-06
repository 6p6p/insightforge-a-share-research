"""In-process background execution manager using asyncio tasks."""

import asyncio
from uuid import UUID

from app.core.logging import get_logger
from app.schemas.workflow import WorkflowRunResponse
from app.workflows.runner import WorkflowRunner

logger = get_logger("app.execution")


class WorkflowExecutionManager:
    """Single-process dev scheduler; not a distributed task queue."""

    def __init__(
        self,
        runner: WorkflowRunner,
        shutdown_timeout_seconds: int = 10,
    ) -> None:
        self._runner = runner
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._closed = False

    async def start_simulation(self, task_id: UUID) -> WorkflowRunResponse:
        if self._closed:
            raise RuntimeError("workflow execution manager is closed")
        run = await self._runner.create_simulation_run(task_id)
        task = asyncio.create_task(
            self._runner.execute_simulation(run.run_id),
            name=f"workflow-{run.run_id}",
        )
        self._tasks[run.run_id] = task
        task.add_done_callback(self._on_task_done)
        return run

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        return await self._runner.get_run(run_id)

    def _on_task_done(self, task: asyncio.Task) -> None:
        name = task.get_name()
        run_id = UUID(name.removeprefix("workflow-")) if name.startswith("workflow-") else None
        if run_id is not None:
            self._tasks.pop(run_id, None)
        try:
            task.exception()  # 消费异常，避免 "Task exception was never retrieved"
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "workflow_task_failed",
                run_id=str(run_id),
                error_type=type(exc).__name__,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = [task for task in self._tasks.values() if not task.done()]
        if not pending:
            self._tasks.clear()
            return
        done, pending = await asyncio.wait(pending, timeout=self._shutdown_timeout_seconds)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
