"""SQLAlchemy model for chunk sets (stage 3A).

ChunkSet 是 ParsedSource 的**确定性分块快照**：记录用哪个 chunker
（chunker_name + chunker_version）、上游解析指纹（source_parse_fingerprint）、
块数（chunk_count）与确定性指纹（chunk_set_fingerprint）。同一
parsed_source + 同一 chunker version + 相同 blocks → 同一 fingerprint →
replay 原 ChunkSet；chunker version 变化 → 新 fingerprint → 新 ChunkSet。

parsed_source_id RESTRICT：上游 ParsedSource 存在期间，ChunkSet 不会被级联
删除，保证 Chunk → ParsedBlock → ParsedSource → SourceRecord → RawArtifact
可完整回溯。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ChunkSetModel(Base):
    __tablename__ = "chunk_sets"
    __table_args__ = (
        CheckConstraint(
            f"source_parse_fingerprint {_SHA256_CHECK}",
            name="ck_chunk_sets_source_parse_fingerprint",
        ),
        CheckConstraint(
            f"chunk_set_fingerprint {_SHA256_CHECK}",
            name="ck_chunk_sets_chunk_set_fingerprint",
        ),
        CheckConstraint(
            "btrim(chunker_name) <> ''",
            name="ck_chunk_sets_chunker_name_not_blank",
        ),
        CheckConstraint(
            "chunker_version >= 1",
            name="ck_chunk_sets_chunker_version",
        ),
        CheckConstraint(
            "chunk_count >= 0",
            name="ck_chunk_sets_chunk_count",
        ),
        UniqueConstraint(
            "chunk_set_fingerprint",
            name="uq_chunk_sets_chunk_set_fingerprint",
        ),
        Index("ix_chunk_sets_parsed_source_id", "parsed_source_id"),
    )

    chunk_set_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parsed_source_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("parsed_sources.parsed_source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunker_name: Mapped[str] = mapped_column(String(64), nullable=False)
    chunker_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    source_parse_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    chunk_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    chunk_set_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
