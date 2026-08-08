"""Data access for document chunks (stage 3A)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.document_chunk import DocumentChunkModel


class DocumentChunkRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, chunk_id: UUID) -> DocumentChunkModel | None:
        result = await self._session.execute(
            select(DocumentChunkModel).where(DocumentChunkModel.chunk_id == chunk_id)
        )
        return result.scalar_one_or_none()

    async def bulk_insert(self, chunks: list[DocumentChunkModel]) -> None:
        """同事务批量插入 chunks；空列表为 no-op。"""
        if not chunks:
            return
        self._session.add_all(chunks)
        await self._session.flush()

    async def list_for_chunk_set(self, chunk_set_id: UUID) -> list[DocumentChunkModel]:
        result = await self._session.execute(
            select(DocumentChunkModel)
            .where(DocumentChunkModel.chunk_set_id == chunk_set_id)
            .order_by(DocumentChunkModel.ordinal.asc())
        )
        return list(result.scalars().all())

    async def count_for_chunk_set(self, chunk_set_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(DocumentChunkModel)
            .where(DocumentChunkModel.chunk_set_id == chunk_set_id)
        )
        return int(result.scalar_one())
