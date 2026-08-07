"""SQLAlchemy model for parsed source blocks (stage 2E.1).

ParsedSourceBlock 是一段可定位的结构化文本：ordinal（1 起、同 ParsedSource
内唯一）、block_type（五类之一）、text（normalize 后非空）、text_sha256
（确定性哈希）、locator（JSONB，指向归档原文 DOM 的稳定定位，用于后续
Evidence 原文核对）。随 ParsedSource 级联删除（FK CASCADE）。

locator 不存绝对路径 / 不存浏览器坐标，只存 DOM 级定位
（type/ordinal/tag/xpath/element_id），保证同一 raw bytes + parser
version 下完全稳定。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import text as sa_text
from sqlalchemy.dialects.postgresql import CHAR, JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_BLOCK_TYPE_CHECK = "block_type IN ('heading','paragraph','list_item','blockquote','table_text')"


class ParsedSourceBlockModel(Base):
    __tablename__ = "parsed_source_blocks"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 1",
            name="ck_parsed_source_blocks_ordinal",
        ),
        CheckConstraint(
            f"text_sha256 {_SHA256_CHECK}",
            name="ck_parsed_source_blocks_text_sha256",
        ),
        CheckConstraint(
            "btrim(text) <> ''",
            name="ck_parsed_source_blocks_text_not_blank",
        ),
        CheckConstraint(
            _BLOCK_TYPE_CHECK,
            name="ck_parsed_source_blocks_block_type",
        ),
        CheckConstraint(
            "jsonb_typeof(locator) = 'object'",
            name="ck_parsed_source_blocks_locator_object",
        ),
        UniqueConstraint(
            "parsed_source_id",
            "ordinal",
            name="uq_parsed_source_blocks_source_ordinal",
        ),
    )

    block_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    parsed_source_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("parsed_sources.parsed_source_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_type: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    locator: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=sa_text("now()")
    )
