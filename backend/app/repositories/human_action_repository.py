"""Data access for human actions."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.human_action import HumanActionModel


class HumanActionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, action: HumanActionModel) -> HumanActionModel:
        self._session.add(action)
        await self._session.flush()
        return action

    async def get_by_run_and_interrupt(
        self,
        run_id: UUID,
        interrupt_key: str,
    ) -> HumanActionModel | None:
        result = await self._session.execute(
            select(HumanActionModel).where(
                HumanActionModel.run_id == run_id,
                HumanActionModel.interrupt_key == interrupt_key,
            )
        )
        return result.scalar_one_or_none()

    async def count_for_run(self, run_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(HumanActionModel)
            .where(HumanActionModel.run_id == run_id)
        )
        return result.scalar_one()
