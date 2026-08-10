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
        """按 fingerprint 查询（普通索引支持；fingerprint **不是 global identity**，
        同指纹可能有多条链，本方法只返回其一，用于审计查询）。"""
        result = await self._session.execute(
            select(MacroTransmissionChainModel)
            .where(MacroTransmissionChainModel.transmission_fingerprint == transmission_fingerprint)
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create(self, chain: MacroTransmissionChainModel) -> MacroTransmissionChainModel:
        """plain INSERT ... RETURNING。

        transmission_fingerprint **不唯一**（migration 0024 移除 global UNIQUE），
        因此不再使用 ON CONFLICT(fingerprint)。链由 MacroClaimService 在**新建
        Claim 的同一事务**内插入（claim_id 是新生成且 UNIQUE，无冲突可能）；replay
        走 get_by_claim_id 复用既有链。**无 update API**（修改 = 新链 = 新行）。
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
            .returning(MacroTransmissionChainModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one()
