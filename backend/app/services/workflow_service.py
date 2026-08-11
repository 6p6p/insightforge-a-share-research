"""Query service for workflow runs and events."""

from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import WorkflowRunNotFound
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowEventResponse, WorkflowRunResponse

_TERMINAL_STATUSES = {"completed", "failed", "cancelled"}


class WorkflowService:
    """Stateless queries; each method uses a short-lived session."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            run = await run_repo.get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        return WorkflowRunResponse.model_validate(run)

    async def list_events_after(
        self,
        run_id: UUID,
        after_event_id: int,
        limit: int = 100,
    ) -> list[WorkflowEventResponse]:
        async with self._sessionmaker() as session:
            event_repo = WorkflowEventRepository(session)
            events = await event_repo.list_after(
                run_id=run_id,
                after_event_id=after_event_id,
                limit=limit,
            )
        return [WorkflowEventResponse.model_validate(event) for event in events]

    async def list_events_after_for_task(
        self,
        task_id: UUID,
        after_event_id: int,
        limit: int = 100,
    ) -> list[WorkflowEventResponse]:
        """跨该任务全部 run 的事件（task 级 SSE 用，全局 event_id cursor）。"""
        async with self._sessionmaker() as session:
            event_repo = WorkflowEventRepository(session)
            events = await event_repo.list_after_for_task(
                task_id=task_id,
                after_event_id=after_event_id,
                limit=limit,
            )
        return [WorkflowEventResponse.model_validate(event) for event in events]

    async def get_latest_run_for_task(self, task_id: UUID) -> WorkflowRunResponse | None:
        """最近一次 run（workspace current_run 投影；无 run 时返回 None）。"""
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            run = await run_repo.get_latest_for_task(task_id)
        if run is None:
            return None
        return WorkflowRunResponse.model_validate(run)

    async def is_task_terminal(self, task_id: UUID) -> bool:
        """任务级终态：**无 active run** 且**至少已有一个 run**。

        注意：waiting_human 属于 active（`get_active_for_task` 覆盖
        pending/running/waiting_human），所以人审等待期间 SSE 保持连接；
        Stage4 完成后、Stage5 尚未创建的空窗期由 execution service 的
        `is_running` 在路由层补判，避免提前断开。
        """
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            active = await run_repo.get_active_for_task(task_id)
            if active is not None:
                return False
            _rows, total = await run_repo.list_for_task(task_id, limit=1, offset=0)
        return total > 0

    async def is_terminal(self, run_id: UUID) -> bool:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            run = await run_repo.get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        return run.status in _TERMINAL_STATUSES
