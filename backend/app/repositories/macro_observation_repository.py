"""Data access for macro observations (stage 2C.2A)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.macro_observation import MacroObservationModel


class MacroObservationRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers.

    Observation 绑定 Snapshot 且不可变：只支持批量插入与查询，不提供 update。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, observation_id: UUID) -> MacroObservationModel | None:
        result = await self._session.execute(
            select(MacroObservationModel).where(
                MacroObservationModel.observation_id == observation_id
            )
        )
        return result.scalar_one_or_none()

    async def bulk_create(self, observations: list[MacroObservationModel]) -> int:
        if not observations:
            return 0
        self._session.add_all(observations)
        await self._session.flush()
        return len(observations)

    async def list_for_snapshot(self, snapshot_id: UUID) -> list[MacroObservationModel]:
        # 稳定排序：normalized_period_start ASC, period ASC, observation_id ASC
        result = await self._session.execute(
            select(MacroObservationModel)
            .where(MacroObservationModel.snapshot_id == snapshot_id)
            .order_by(
                MacroObservationModel.normalized_period_start.asc(),
                MacroObservationModel.period.asc(),
                MacroObservationModel.observation_id.asc(),
            )
        )
        return list(result.scalars().all())

    async def count_for_snapshot(self, snapshot_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(MacroObservationModel)
            .where(MacroObservationModel.snapshot_id == snapshot_id)
        )
        return int(result.scalar_one())
