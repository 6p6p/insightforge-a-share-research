"""Data access for financial calculations (stage 4B.2B)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.financial_calculation import FinancialCalculationModel


class FinancialCalculationRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, calculation_id: object) -> FinancialCalculationModel | None:
        result = await self._session.execute(
            select(FinancialCalculationModel).where(
                FinancialCalculationModel.calculation_id == calculation_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(
        self, calculation_fingerprint: str
    ) -> FinancialCalculationModel | None:
        result = await self._session.execute(
            select(FinancialCalculationModel).where(
                FinancialCalculationModel.calculation_fingerprint == calculation_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, calculation: FinancialCalculationModel
    ) -> tuple[FinancialCalculationModel, bool]:
        """INSERT ... ON CONFLICT(calculation_fingerprint) DO NOTHING RETURNING。

        并发下相同 Calculation（同一 calculation_fingerprint）只能有 1 行：输家
        回查既有行（created=False）并复用（replay 语义）。**无 Python 进程锁**；
        不允许 update API（修改 = 新 calculation = 新 fingerprint = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(calculation, column.key)
            for column in FinancialCalculationModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(FinancialCalculationModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[FinancialCalculationModel.calculation_fingerprint]
            )
            .returning(FinancialCalculationModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(calculation.calculation_fingerprint)
        if existing is None:
            raise RuntimeError("financial calculation conflict without existing row")
        return existing, False
