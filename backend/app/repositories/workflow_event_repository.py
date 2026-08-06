"""Data access for workflow events."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.workflow_event import WorkflowEventModel


class WorkflowEventRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, event: WorkflowEventModel) -> WorkflowEventModel:
        self._session.add(event)
        await self._session.flush()
        return event

    async def list_after(
        self,
        *,
        run_id: UUID,
        after_event_id: int,
        limit: int,
    ) -> list[WorkflowEventModel]:
        result = await self._session.execute(
            select(WorkflowEventModel)
            .where(
                WorkflowEventModel.run_id == run_id,
                WorkflowEventModel.event_id > after_event_id,
            )
            .order_by(WorkflowEventModel.event_id.asc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_latest_event_id(self, run_id: UUID) -> int | None:
        result = await self._session.execute(
            select(func.max(WorkflowEventModel.event_id)).where(WorkflowEventModel.run_id == run_id)
        )
        return result.scalar_one_or_none()

    async def count_for_run(self, run_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(WorkflowEventModel)
            .where(WorkflowEventModel.run_id == run_id)
        )
        return result.scalar_one()
