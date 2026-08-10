"""SQLAlchemy model for evidence-bound section drafts (stage 5B).

`draft_sections` 保存一次 **已验证 ReportOutline 的单个 section** 的不可变正文
草稿。Writer 只消费 `VerifiedReportOutline`（5A Gate 1）派生的 section 输入，
经 Evidence-bound Writer（DeepSeek V4 Flash，structured output）生成一节正文。

- outline_id FK `report_outlines.outline_id` RESTRICT——outline 存在期间草稿不
  静默消失；草稿不可变，无 update API；
- section_id / section_order / section_type / title：outline section 身份快照
  （不重写，供检索与审计）；
- section_schema_version / writer_name / writer_version / writer_model_id：
  writer 身份（当前 = DRAFT_SECTION_SCHEMA_VERSION / evidence_bound_section_writer /
  1 / deepseek:deepseek-v4-flash）；
- writer_input_fingerprint UNIQUE：LLM 输入边界的确定性指纹；同输入 → replay
  同一行（**0 model calls**）；输入变化 → 新指纹 → 新草稿（旧行保留）；
- section_payload JSONB（v1 = `{"paragraphs":[...]}`，只存真实 Claim/Evidence
  UUID + conflict/gap indexes，不存 alias / prompt / raw provider response）；
- section_fingerprint UNIQUE：writer_input_fingerprint + normalized payload 的
  SHA-256，replay 校验用。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class DraftSectionModel(Base):
    __tablename__ = "draft_sections"
    __table_args__ = (
        CheckConstraint(
            f"writer_input_fingerprint {_SHA256_CHECK}",
            name="ck_draft_sections_writer_input_fingerprint",
        ),
        CheckConstraint(
            f"section_fingerprint {_SHA256_CHECK}",
            name="ck_draft_sections_section_fingerprint",
        ),
        CheckConstraint(
            "section_schema_version >= 1",
            name="ck_draft_sections_section_schema_version",
        ),
        CheckConstraint("section_order >= 1", name="ck_draft_sections_section_order"),
        CheckConstraint("writer_version >= 1", name="ck_draft_sections_writer_version"),
        CheckConstraint("btrim(section_id) <> ''", name="ck_draft_sections_section_id_not_blank"),
        CheckConstraint("btrim(title) <> ''", name="ck_draft_sections_title_not_blank"),
        CheckConstraint("btrim(writer_name) <> ''", name="ck_draft_sections_writer_name_not_blank"),
        CheckConstraint(
            "btrim(writer_model_id) <> ''", name="ck_draft_sections_writer_model_id_not_blank"
        ),
        UniqueConstraint(
            "writer_input_fingerprint",
            name="uq_draft_sections_writer_input_fingerprint",
        ),
        UniqueConstraint("section_fingerprint", name="uq_draft_sections_section_fingerprint"),
        Index("ix_draft_sections_outline_id", "outline_id"),
        Index("ix_draft_sections_outline_section", "outline_id", "section_order"),
    )

    draft_section_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    outline_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_outlines.outline_id", ondelete="RESTRICT"),
        nullable=False,
    )
    section_id: Mapped[str] = mapped_column(String, nullable=False)
    section_order: Mapped[int] = mapped_column(Integer, nullable=False)
    section_type: Mapped[str] = mapped_column(String, nullable=False)
    title: Mapped[str] = mapped_column(String, nullable=False)
    section_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    writer_name: Mapped[str] = mapped_column(String, nullable=False)
    writer_version: Mapped[int] = mapped_column(Integer, nullable=False)
    writer_model_id: Mapped[str] = mapped_column(String, nullable=False)
    writer_input_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    section_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    section_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
