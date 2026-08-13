"""Data access for chunk vector index manifests (stage 3B.1)."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.chunk_vector_index import ChunkVectorIndexModel


class ChunkVectorIndexRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, vector_index_id: UUID) -> ChunkVectorIndexModel | None:
        result = await self._session.execute(
            select(ChunkVectorIndexModel).where(
                ChunkVectorIndexModel.vector_index_id == vector_index_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_identity(
        self,
        chunk_set_id: UUID,
        embedding_model_id: str,
        embedding_model_revision: str,
        collection_schema_version: int,
    ) -> ChunkVectorIndexModel | None:
        """按自然身份精确定位 manifest（同 ChunkSet + 同模型配置 → 最多 1 行）。"""
        result = await self._session.execute(
            select(ChunkVectorIndexModel).where(
                ChunkVectorIndexModel.chunk_set_id == chunk_set_id,
                ChunkVectorIndexModel.embedding_model_id == embedding_model_id,
                ChunkVectorIndexModel.embedding_model_revision == embedding_model_revision,
                ChunkVectorIndexModel.collection_schema_version == collection_schema_version,
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, index: ChunkVectorIndexModel
    ) -> tuple[ChunkVectorIndexModel, bool]:
        """INSERT ... ON CONFLICT(自然身份) DO NOTHING RETURNING。

        并发重建同 ChunkSet + 同模型配置：输家回查既有 manifest（created=False），
        双方共享同一行做确定性 upsert（幂等），最终仍只有 1 个 manifest。
        """
        excluded = {"created_at", "ready_at"}
        values = {
            column.key: getattr(index, column.key)
            for column in ChunkVectorIndexModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ChunkVectorIndexModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[
                    ChunkVectorIndexModel.chunk_set_id,
                    ChunkVectorIndexModel.embedding_model_id,
                    ChunkVectorIndexModel.embedding_model_revision,
                    ChunkVectorIndexModel.collection_schema_version,
                ]
            )
            .returning(ChunkVectorIndexModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_identity(
            index.chunk_set_id,
            index.embedding_model_id,
            index.embedding_model_revision,
            index.collection_schema_version,
        )
        if existing is None:
            raise RuntimeError("chunk vector index conflict without existing row")
        return existing, False

    async def reset_to_building(
        self, vector_index_id: UUID, *, index_fingerprint: str | None = None
    ) -> None:
        """retry failed/building（或 eval force_rebuild）：回到 building，清空上一次
        错误与计数；`index_fingerprint` 可选——重建进不同 collection 时同步更新
        fingerprint（否则 manifest 残留旧 collection 的指纹，内部不一致）。"""
        values: dict = {
            "status": "building",
            "last_error_code": None,
            "indexed_chunk_count": 0,
        }
        if index_fingerprint is not None:
            values["index_fingerprint"] = index_fingerprint
        await self._session.execute(
            update(ChunkVectorIndexModel)
            .where(ChunkVectorIndexModel.vector_index_id == vector_index_id)
            .values(**values)
        )

    async def mark_ready(
        self, vector_index_id: UUID, *, indexed: int, collection_name: str | None = None
    ) -> None:
        """mark ready；`collection_name` 可选：eval per-attempt 重建时把 manifest 指向
        实际写入的 collection（生产路径同名，无副作用）。"""
        values: dict = {
            "status": "ready",
            "indexed_chunk_count": indexed,
            "last_error_code": None,
            "ready_at": datetime.now(UTC),
        }
        if collection_name is not None:
            values["collection_name"] = collection_name
        await self._session.execute(
            update(ChunkVectorIndexModel)
            .where(ChunkVectorIndexModel.vector_index_id == vector_index_id)
            .values(**values)
        )

    async def mark_failed(self, vector_index_id: UUID, *, error_code: str) -> None:
        await self._session.execute(
            update(ChunkVectorIndexModel)
            .where(ChunkVectorIndexModel.vector_index_id == vector_index_id)
            .values(
                status="failed",
                last_error_code=error_code,
            )
        )
