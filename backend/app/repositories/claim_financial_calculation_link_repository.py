"""Data access for claim ↔ financial calculation links (stage 4B.2C.1)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim_financial_calculation_link import ClaimFinancialCalculationLinkModel


class ClaimFinancialCalculationLinkRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_claim(self, claim_id: UUID) -> list[ClaimFinancialCalculationLinkModel]:
        result = await self._session.execute(
            select(ClaimFinancialCalculationLinkModel).where(
                ClaimFinancialCalculationLinkModel.claim_id == claim_id
            )
        )
        return list(result.scalars().all())

    async def bulk_insert(self, links: list[ClaimFinancialCalculationLinkModel]) -> None:
        if links:
            self._session.add_all(links)
