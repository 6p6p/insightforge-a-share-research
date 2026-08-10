"""Data access for macro transmission ↔ evidence links (stage 4C.1A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.macro_transmission_evidence_link import (
    MacroTransmissionEvidenceLinkModel,
)


class MacroTransmissionEvidenceLinkRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_by_transmission(
        self, transmission_id: UUID
    ) -> list[MacroTransmissionEvidenceLinkModel]:
        result = await self._session.execute(
            select(MacroTransmissionEvidenceLinkModel).where(
                MacroTransmissionEvidenceLinkModel.transmission_id == transmission_id
            )
        )
        return list(result.scalars().all())

    async def bulk_insert(self, links: list[MacroTransmissionEvidenceLinkModel]) -> None:
        if links:
            self._session.add_all(links)
