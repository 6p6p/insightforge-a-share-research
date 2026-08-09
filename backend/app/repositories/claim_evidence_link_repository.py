"""Data access for claim ↔ evidence links (stage 4A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel


class ClaimEvidenceLinkRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_claim(self, claim_id: UUID) -> list[ClaimEvidenceLinkModel]:
        result = await self._session.execute(
            select(ClaimEvidenceLinkModel).where(ClaimEvidenceLinkModel.claim_id == claim_id)
        )
        return list(result.scalars().all())

    async def bulk_insert(self, links: list[ClaimEvidenceLinkModel]) -> None:
        if links:
            self._session.add_all(links)

    async def count_for_claim(self, claim_id: UUID) -> int:
        result = await self._session.execute(
            select(ClaimEvidenceLinkModel).where(ClaimEvidenceLinkModel.claim_id == claim_id)
        )
        return len(list(result.scalars().all()))
