"""SQLAlchemy model for parsed sources (stage 2E.1).

ParsedSource 是 SourceRecord 的**确定性解析快照**：记录用哪个 parser
（parser_name + parser_version）、哪份 raw bytes（raw_content_sha256 +
artifact_id）解析出的结构化文本摘要（extracted_title /
extracted_published_at）、块数（block_count）与确定性指纹
（parse_fingerprint）。它不是 Chunk、不是 Evidence、不更新 SourceRecord
元数据。同一 source + 相同 raw bytes + 相同 parser version → 同一
fingerprint → replay 原快照；parser version 变化 → 新 fingerprint →
新快照。

source_id / artifact_id 均 RESTRICT：上游 SourceRecord / RawArtifact 存在
期间，解析快照不会被级联删除，保证证据链可追溯。
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import CHAR
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ParsedSourceModel(Base):
    __tablename__ = "parsed_sources"
    __table_args__ = (
        CheckConstraint(
            f"raw_content_sha256 {_SHA256_CHECK}",
            name="ck_parsed_sources_raw_content_sha256",
        ),
        CheckConstraint(
            f"parse_fingerprint {_SHA256_CHECK}",
            name="ck_parsed_sources_parse_fingerprint",
        ),
        CheckConstraint(
            "btrim(parser_name) <> ''",
            name="ck_parsed_sources_parser_name_not_blank",
        ),
        CheckConstraint(
            "parser_version >= 1",
            name="ck_parsed_sources_parser_version",
        ),
        CheckConstraint(
            "block_count >= 0",
            name="ck_parsed_sources_block_count",
        ),
        UniqueConstraint(
            "parse_fingerprint",
            name="uq_parsed_sources_parse_fingerprint",
        ),
        Index("ix_parsed_sources_source_id", "source_id"),
        Index("ix_parsed_sources_artifact_id", "artifact_id"),
    )

    parsed_source_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("source_records.source_id", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    parser_name: Mapped[str] = mapped_column(String(64), nullable=False)
    parser_version: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    parse_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    extracted_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    block_count: Mapped[int] = mapped_column(BigInteger, nullable=False)
    parsed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
