"""Data access for parsed source blocks (stage 2E.1)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.parsed_source_block import ParsedSourceBlockModel


class ParsedSourceBlockRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_insert(self, blocks: list[ParsedSourceBlockModel]) -> None:
        """同事务批量插入 blocks；空列表为 no-op。"""
        if not blocks:
            return
        self._session.add_all(blocks)
        await self._session.flush()

    async def list_for_parsed_source(
        self,
        parsed_source_id: UUID,
    ) -> list[ParsedSourceBlockModel]:
        result = await self._session.execute(
            select(ParsedSourceBlockModel)
            .where(ParsedSourceBlockModel.parsed_source_id == parsed_source_id)
            .order_by(ParsedSourceBlockModel.ordinal.asc())
        )
        return list(result.scalars().all())

    async def count_for_parsed_source(self, parsed_source_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(ParsedSourceBlockModel)
            .where(ParsedSourceBlockModel.parsed_source_id == parsed_source_id)
        )
        return int(result.scalar_one())
