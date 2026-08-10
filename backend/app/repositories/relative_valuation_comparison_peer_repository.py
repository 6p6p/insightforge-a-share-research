"""Data access for relative valuation comparison peer links (stage 4C.2A)."""

from collections.abc import Iterable
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.relative_valuation_comparison_peer import (
    RelativeValuationComparisonPeerModel,
)


class RelativeValuationComparisonPeerRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_insert(self, links: Iterable[RelativeValuationComparisonPeerModel]) -> None:
        self._session.add_all(list(links))
        await self._session.flush()

    async def list_by_comparison(
        self, comparison_id: UUID
    ) -> list[RelativeValuationComparisonPeerModel]:
        result = await self._session.execute(
            select(RelativeValuationComparisonPeerModel)
            .where(RelativeValuationComparisonPeerModel.comparison_id == comparison_id)
            .order_by(
                RelativeValuationComparisonPeerModel.peer_company_id.asc(),
                RelativeValuationComparisonPeerModel.peer_observation_id.asc(),
            )
        )
        return list(result.scalars().all())

    async def list_by_observation(
        self, peer_observation_id: UUID
    ) -> list[RelativeValuationComparisonPeerModel]:
        result = await self._session.execute(
            select(RelativeValuationComparisonPeerModel).where(
                RelativeValuationComparisonPeerModel.peer_observation_id == peer_observation_id
            )
        )
        return list(result.scalars().all())
