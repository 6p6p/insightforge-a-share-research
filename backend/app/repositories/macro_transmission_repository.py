"""Data access for macro transmission chains (stage 4C.1B)."""

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

    async def list_by_fingerprint(
        self, transmission_fingerprint: str
    ) -> tuple[MacroTransmissionChainModel, ...]:
        """按 fingerprint 查询全部匹配链（普通索引支持；fingerprint **不是 global
        identity**，同指纹可能有多条链）。**不做 `.limit(1)`**——审计需要完整集合；
        按 created_at / transmission_id 稳定排序（确定性顺序，可复现）。
        """
        result = await self._session.execute(
            select(MacroTransmissionChainModel)
            .where(MacroTransmissionChainModel.transmission_fingerprint == transmission_fingerprint)
            .order_by(
                MacroTransmissionChainModel.created_at.asc(),
                MacroTransmissionChainModel.transmission_id.asc(),
            )
        )
        return tuple(result.scalars().all())

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
