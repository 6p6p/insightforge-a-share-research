"""Data access for research tasks."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.research_task import ResearchTaskModel
from app.domain.tasks import TaskStatus


class ResearchTaskRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def create(self, task: ResearchTaskModel) -> ResearchTaskModel:
        self._session.add(task)
        await self._session.flush()
        return task

    async def get_by_id(self, task_id: UUID) -> ResearchTaskModel | None:
        result = await self._session.execute(
            select(ResearchTaskModel).where(ResearchTaskModel.task_id == task_id)
        )
        return result.scalar_one_or_none()

    async def get_by_idempotency_key(self, key: str) -> ResearchTaskModel | None:
        result = await self._session.execute(
            select(ResearchTaskModel).where(ResearchTaskModel.idempotency_key == key)
        )
        return result.scalar_one_or_none()

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> tuple[list[ResearchTaskModel], int]:
        query = select(ResearchTaskModel)
        count_query = select(func.count()).select_from(ResearchTaskModel)
        if status is not None:
            query = query.where(ResearchTaskModel.status == status.value)
            count_query = count_query.where(ResearchTaskModel.status == status.value)
        total = (await self._session.execute(count_query)).scalar_one()
        query = (
            query.order_by(
                ResearchTaskModel.created_at.desc(),
                ResearchTaskModel.task_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(query)).scalars().all()
        return list(rows), total
