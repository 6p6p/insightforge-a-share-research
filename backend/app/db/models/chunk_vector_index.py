"""SQLAlchemy model for chunk vector index manifests (stage 3B.1).

`chunk_vector_indexes` 是每个 (ChunkSet, embedding 模型配置) 的**可重建向量
索引清单**：PostgreSQL 是 Source of Truth，Chroma 是 derived index。该表只登记
"哪个 ChunkSet 用哪个模型配置、期望多少 chunk、实际索引多少、指向哪个
collection、指纹是多少"，不存 embedding 本身。

- 自然身份 UNIQUE(chunk_set_id, embedding_model_id, embedding_model_revision,
  collection_schema_version)：同 ChunkSet + 同模型配置 → 最多 1 个 manifest，
  并发重建共享同一行（确定性 id + upsert 幂等）。
- index_fingerprint 不含 timestamps / DB ID / status，只含可重建语义的字段。
- chunk_set_id RESTRICT：上游 ChunkSet 存在期间，manifest 不会被级联删除。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_STATUS_CHECK = "status IN ('building','ready','failed')"


class ChunkVectorIndexModel(Base):
    __tablename__ = "chunk_vector_indexes"
    __table_args__ = (
        CheckConstraint(
            f"index_fingerprint {_SHA256_CHECK}",
            name="ck_chunk_vector_indexes_fingerprint",
        ),
        CheckConstraint(
            "embedding_dimension > 0",
            name="ck_chunk_vector_indexes_dimension",
        ),
        CheckConstraint(
            "expected_chunk_count >= 0",
            name="ck_chunk_vector_indexes_expected_count",
        ),
        CheckConstraint(
            "indexed_chunk_count >= 0",
            name="ck_chunk_vector_indexes_indexed_count",
        ),
        CheckConstraint(
            "indexed_chunk_count <= expected_chunk_count",
            name="ck_chunk_vector_indexes_indexed_lte_expected",
        ),
        CheckConstraint(_STATUS_CHECK, name="ck_chunk_vector_indexes_status"),
        CheckConstraint(
            "btrim(embedding_model_id) <> ''",
            name="ck_chunk_vector_indexes_model_id_not_blank",
        ),
        CheckConstraint(
            "btrim(embedding_model_revision) <> ''",
            name="ck_chunk_vector_indexes_model_revision_not_blank",
        ),
        CheckConstraint(
            "btrim(collection_name) <> ''",
            name="ck_chunk_vector_indexes_collection_name_not_blank",
        ),
        UniqueConstraint(
            "index_fingerprint",
            name="uq_chunk_vector_indexes_fingerprint",
        ),
        UniqueConstraint(
            "chunk_set_id",
            "embedding_model_id",
            "embedding_model_revision",
            "collection_schema_version",
            name="uq_chunk_vector_indexes_identity",
        ),
        Index("ix_chunk_vector_indexes_chunk_set_id", "chunk_set_id"),
    )

    vector_index_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    chunk_set_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chunk_sets.chunk_set_id", ondelete="RESTRICT"),
        nullable=False,
    )
    embedding_model_id: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_model_revision: Mapped[str] = mapped_column(String(200), nullable=False)
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False)
    normalize_embeddings: Mapped[bool] = mapped_column(Boolean, nullable=False)
    collection_name: Mapped[str] = mapped_column(String(200), nullable=False)
    collection_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_chunk_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    indexed_chunk_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    index_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default=text("'building'")
    )
    last_error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
