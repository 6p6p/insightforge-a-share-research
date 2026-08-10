"""Data access for valuation metric observations (stage 4C.2A)."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.valuation_metric_observation import ValuationMetricObservationModel


class ValuationMetricObservationRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self, valuation_observation_id: object
    ) -> ValuationMetricObservationModel | None:
        result = await self._session.execute(
            select(ValuationMetricObservationModel).where(
                ValuationMetricObservationModel.valuation_observation_id == valuation_observation_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(
        self, valuation_observation_fingerprint: str
    ) -> ValuationMetricObservationModel | None:
        result = await self._session.execute(
            select(ValuationMetricObservationModel).where(
                ValuationMetricObservationModel.valuation_observation_fingerprint
                == valuation_observation_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def list_by_ids(
        self, observation_ids: Iterable[UUID]
    ) -> dict[UUID, ValuationMetricObservationModel]:
        """按 id 批量加载 observation（返回 dict，缺失者不在 dict 中）。"""
        ids = list(observation_ids)
        if not ids:
            return {}
        result = await self._session.execute(
            select(ValuationMetricObservationModel).where(
                ValuationMetricObservationModel.valuation_observation_id.in_(ids)
            )
        )
        return {row.valuation_observation_id: row for row in result.scalars().all()}

    async def create_or_get(
        self, observation: ValuationMetricObservationModel
    ) -> tuple[ValuationMetricObservationModel, bool]:
        """INSERT ... ON CONFLICT(valuation_observation_fingerprint) DO NOTHING
        RETURNING。

        并发下相同 Observation（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（修改 = 新 observation = 新 fingerprint = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(observation, column.key)
            for column in ValuationMetricObservationModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ValuationMetricObservationModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[ValuationMetricObservationModel.valuation_observation_fingerprint]
            )
            .returning(ValuationMetricObservationModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(observation.valuation_observation_fingerprint)
        if existing is None:
            raise RuntimeError("valuation observation conflict without existing row")
        return existing, False
