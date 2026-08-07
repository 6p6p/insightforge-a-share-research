"""SQLAlchemy model for document chunks (stage 3A).

DocumentChunk 是一个确定性文本块（字符窗口）：ordinal（1 起、同 ChunkSet
内唯一）、text（非空）、text_sha256（确定性哈希）、char_count、
locator_refs（JSONB 数组，每个元素指向一个原 ParsedBlock 文本片段：
block_ordinal + 相对原 block.text 的 char 索引 [start, end) + 原 locator）。
随 ChunkSet 级联删除（FK CASCADE）。

locator_refs 不存绝对路径，只存确定性定位，保证同一 ParsedSource +
chunker version 下完全稳定，且可回溯到归档原文。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class DocumentChunkModel(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 1",
            name="ck_document_chunks_ordinal",
        ),
        CheckConstraint(
            f"text_sha256 {_SHA256_CHECK}",
            name="ck_document_chunks_text_sha256",
        ),
        CheckConstraint(
            "char_count >= 1",
            name="ck_document_chunks_char_count",
        ),
        CheckConstraint(
            "btrim(text) <> ''",
            name="ck_document_chunks_text_not_blank",
        ),
        CheckConstraint(
            "jsonb_typeof(locator_refs) = 'array'",
            name="ck_document_chunks_locator_refs_array",
        ),
        UniqueConstraint(
            "chunk_set_id",
            "ordinal",
            name="uq_document_chunks_set_ordinal",
        ),
    )

    chunk_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    chunk_set_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("chunk_sets.chunk_set_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    char_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    locator_refs: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
