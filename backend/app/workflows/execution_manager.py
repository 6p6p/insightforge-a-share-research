"""In-process background execution manager using asyncio tasks."""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    WorkflowRunAlreadyFinished,
    WorkflowRunNotFound,
)
from app.core.logging import get_logger
from app.db.models.workflow_event import WorkflowEventModel
from app.domain.tasks import HumanActionType, WorkflowEventType
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowRunResponse
from app.workflows.runner import WorkflowRunner

logger = get_logger("app.execution")


class WorkflowExecutionManager:
    """Single-process dev scheduler; not a distributed task queue."""

    def __init__(
        self,
        runner: WorkflowRunner,
        shutdown_timeout_seconds: int = 10,
        sessionmaker: async_sessionmaker | None = None,
    ) -> None:
        self._runner = runner
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._sessionmaker = sessionmaker
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._closed = False

    async def start_simulation(self, task_id: UUID) -> WorkflowRunResponse:
        if self._closed:
            raise RuntimeError("workflow execution manager is closed")
        run = await self._runner.create_simulation_run(task_id)
        self._schedule(run.run_id, self._runner.execute_simulation(run.run_id))
        return run

    async def resume_simulation(
        self,
        run_id: UUID,
        action_type: HumanActionType,
    ) -> WorkflowRunResponse:
        if self._closed:
            raise RuntimeError("workflow execution manager is closed")
        # 先原子接受（claim + HumanAction + run_resumed 同一事务）；
        # 失败直接抛出，不创建后台 Task
        preparation = await self._runner.prepare_resume(run_id, action_type)
        # 接受成功后才调度 Graph 恢复
        self._schedule(run_id, self._runner.continue_resume(preparation))
        return await self._runner.get_run(run_id)

    async def cancel_run(self, run_id: UUID) -> WorkflowRunResponse:
        if self._closed:
            raise RuntimeError("workflow execution manager is closed")
        task = self._tasks.get(run_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # runner 可能已标记 failed（竞态）；终态由 DB 原子更新裁决
                pass
        if self._sessionmaker is None:
            raise RuntimeError("execution manager has no session factory")
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            updated = await run_repo.mark_cancelled(run_id, datetime.now(UTC))
            if updated is None:
                raise WorkflowRunAlreadyFinished()
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_CANCELLED.value,
                    message="工作流运行已取消",
                    payload={},
                )
            )
            await session.commit()
        return WorkflowRunResponse.model_validate(updated)

    async def retry_run(self, run_id: UUID) -> WorkflowRunResponse:
        if self._closed:
            raise RuntimeError("workflow execution manager is closed")
        if self._sessionmaker is None:
            raise RuntimeError("execution manager has no session factory")
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            original = await run_repo.get_by_id(run_id)
        if original is None:
            raise WorkflowRunNotFound()
        if original.status not in ("failed", "cancelled"):
            raise WorkflowRunAlreadyFinished()
        return await self.start_simulation(original.task_id)

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        return await self._runner.get_run(run_id)

    def _schedule(self, run_id: UUID, coroutine) -> None:
        task = asyncio.create_task(coroutine, name=f"workflow-{run_id}")
        self._tasks[run_id] = task
        task.add_done_callback(self._on_task_done)

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
