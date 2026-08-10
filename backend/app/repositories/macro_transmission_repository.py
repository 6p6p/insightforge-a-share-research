"""Data access for macro transmission chains (stage 4C.1A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.macro_transmission_chain import MacroTransmissionChainModel


class MacroTransmissionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, transmission_id: UUID) -> MacroTransmissionChainModel | None:
        result = await self._session.execute(
            select(MacroTransmissionChainModel).where(
                MacroTransmissionChainModel.transmission_id == transmission_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_claim_id(self, claim_id: UUID) -> MacroTransmissionChainModel | None:
        result = await self._session.execute(
            select(MacroTransmissionChainModel).where(
                MacroTransmissionChainModel.claim_id == claim_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(
        self, transmission_fingerprint: str
    ) -> MacroTransmissionChainModel | None:
        result = await self._session.execute(
            select(MacroTransmissionChainModel).where(
                MacroTransmissionChainModel.transmission_fingerprint == transmission_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, chain: MacroTransmissionChainModel
    ) -> tuple[MacroTransmissionChainModel, bool]:
        """INSERT ... ON CONFLICT(transmission_fingerprint) DO NOTHING RETURNING。

        并发下相同传导链（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（修改 = 新传导链 = 新 fingerprint = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(chain, column.key)
            for column in MacroTransmissionChainModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(MacroTransmissionChainModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[MacroTransmissionChainModel.transmission_fingerprint]
            )
            .returning(MacroTransmissionChainModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(chain.transmission_fingerprint)
        if existing is None:
            raise RuntimeError("macro transmission conflict without existing row")
        return existing, False
