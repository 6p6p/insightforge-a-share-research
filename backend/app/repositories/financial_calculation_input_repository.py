"""Data access for financial calculation inputs (stage 4B.2B).

`financial_calculation_inputs` 绑定 calculation → Observation（每个 input_role
恰好一行），形成 Calculation → Observation → EvidenceCard → Source 证据链。
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.financial_calculation import FinancialCalculationInputModel


class FinancialCalculationInputRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_inputs(self, bindings: list[FinancialCalculationInputModel]) -> None:
        """批量插入 inputs（PK(calculation_id, input_role)；调用方保证角色唯一）。"""
        self._session.add_all(bindings)

    async def get_by_calculation_id(
        self, calculation_id: object
    ) -> list[FinancialCalculationInputModel]:
        result = await self._session.execute(
            select(FinancialCalculationInputModel).where(
                FinancialCalculationInputModel.calculation_id == calculation_id
            )
        )
        return list(result.scalars().all())
