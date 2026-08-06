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

    async def is_terminal(self, run_id: UUID) -> bool:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            run = await run_repo.get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        return run.status in _TERMINAL_STATUSES
