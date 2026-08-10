"""Data access for claim synthesis run ↔ claim input links (stage 4D.1A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim_synthesis_input_link import ClaimSynthesisInputLinkModel


class ClaimSynthesisInputLinkRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_insert(self, links: list[ClaimSynthesisInputLinkModel]) -> None:
        if links:
            self._session.add_all(links)

    async def list_by_synthesis(self, synthesis_id: UUID) -> list[ClaimSynthesisInputLinkModel]:
        result = await self._session.execute(
            select(ClaimSynthesisInputLinkModel).where(
                ClaimSynthesisInputLinkModel.synthesis_id == synthesis_id
            )
        )
        return list(result.scalars().all())

    async def list_by_claim(self, claim_id: UUID) -> list[ClaimSynthesisInputLinkModel]:
        result = await self._session.execute(
            select(ClaimSynthesisInputLinkModel).where(
                ClaimSynthesisInputLinkModel.claim_id == claim_id
            )
        )
        return list(result.scalars().all())
