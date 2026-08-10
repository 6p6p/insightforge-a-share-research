"""Data access for relative valuation claim profiles (stage 4C.2B.1)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.relative_valuation_claim_profile import RelativeValuationClaimProfileModel


class RelativeValuationClaimProfileRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_claim(self, claim_id: UUID) -> RelativeValuationClaimProfileModel | None:
        result = await self._session.execute(
            select(RelativeValuationClaimProfileModel).where(
                RelativeValuationClaimProfileModel.claim_id == claim_id
            )
        )
        return result.scalar_one_or_none()

    async def create(self, profile: RelativeValuationClaimProfileModel) -> None:
        self._session.add(profile)
        await self._session.flush()
