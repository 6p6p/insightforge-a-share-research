"""Data access for financial metric observations (stage 4B.2A)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.financial_metric_observation import FinancialMetricObservationModel


class FinancialMetricObservationRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(
        self, metric_observation_id: object
    ) -> FinancialMetricObservationModel | None:
        result = await self._session.execute(
            select(FinancialMetricObservationModel).where(
                FinancialMetricObservationModel.metric_observation_id == metric_observation_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(
        self, metric_fingerprint: str
    ) -> FinancialMetricObservationModel | None:
        result = await self._session.execute(
            select(FinancialMetricObservationModel).where(
                FinancialMetricObservationModel.metric_fingerprint == metric_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, observation: FinancialMetricObservationModel
    ) -> tuple[FinancialMetricObservationModel, bool]:
        """INSERT ... ON CONFLICT(metric_fingerprint) DO NOTHING RETURNING。

        并发下相同 Observation（同一 metric_fingerprint）只能有 1 行：输家回查
        既有行（created=False）并复用（replay 语义）。**无 Python 进程锁**；
        不允许 update API（修改 = 新 observation = 新 fingerprint = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(observation, column.key)
            for column in FinancialMetricObservationModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(FinancialMetricObservationModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[FinancialMetricObservationModel.metric_fingerprint]
            )
            .returning(FinancialMetricObservationModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(observation.metric_fingerprint)
        if existing is None:
            raise RuntimeError("financial metric observation conflict without existing row")
        return existing, False
