"""Data access for claim ↔ relative valuation comparison links (stage 4C.2B.1)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim_relative_valuation_comparison_link import (
    ClaimRelativeValuationComparisonLinkModel,
)


class ClaimRelativeValuationComparisonLinkRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_claim(
        self, claim_id: UUID
    ) -> list[ClaimRelativeValuationComparisonLinkModel]:
        result = await self._session.execute(
            select(ClaimRelativeValuationComparisonLinkModel).where(
                ClaimRelativeValuationComparisonLinkModel.claim_id == claim_id
            )
        )
        return list(result.scalars().all())

    async def bulk_insert(self, links: list[ClaimRelativeValuationComparisonLinkModel]) -> None:
        if links:
            self._session.add_all(links)
