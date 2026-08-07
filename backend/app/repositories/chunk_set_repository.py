"""Data access for chunk sets (stage 3A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chunk_set import ChunkSetModel


class ChunkSetRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_fingerprint(self, chunk_set_fingerprint: str) -> ChunkSetModel | None:
        result = await self._session.execute(
            select(ChunkSetModel).where(
                ChunkSetModel.chunk_set_fingerprint == chunk_set_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def get_by_parsed_source_id(self, parsed_source_id: UUID) -> ChunkSetModel | None:
        result = await self._session.execute(
            select(ChunkSetModel).where(ChunkSetModel.parsed_source_id == parsed_source_id)
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, chunk_set: ChunkSetModel) -> tuple[ChunkSetModel, bool]:
        """INSERT ... ON CONFLICT(chunk_set_fingerprint) DO NOTHING RETURNING。

        并发下相同 chunking 只能有 1 个 ChunkSet：输家回查既有行
        （created=False）并复用其 chunks（replay 语义）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(chunk_set, column.key)
            for column in ChunkSetModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ChunkSetModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ChunkSetModel.chunk_set_fingerprint])
            .returning(ChunkSetModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(chunk_set.chunk_set_fingerprint)
        if existing is None:
            raise RuntimeError("chunk set conflict without existing row")
        return existing, False
