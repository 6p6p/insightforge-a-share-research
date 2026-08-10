"""SQLAlchemy model for deterministic report outlines (stage 5A).

`report_outlines` 保存一次 **不可变、确定性派生**的报告提纲：把已验证的
SynthesisResult（claim_synthesis_results）机械地映射为结构化提纲，**不调用
LLM 规划**（0 planner model / 0 analyst version）。提纲不是 Report /
DraftSection / Audit 正文。

- synthesis_result_id FK `claim_synthesis_results.synthesis_result_id` RESTRICT
  ——result 存在期间提纲不静默消失；提纲不可变，无 update API；
- company_id / research_question_sha256 / analysis_as_of 派生自 synthesis run，
  供检索；
- outline_schema_version / outline_fingerprint 由 ReportOutlineService 确定性
  派生；outline_payload 为 JSONB（v1 = `{"sections":[...]}`）；
- outline_fingerprint UNIQUE：同 result + 同 schema + 同 payload → replay 同一
  行；SynthesisResult 变化 → 新指纹 → 新提纲（旧行保留）；
- **不存** prompt / raw provider response / Report 正文 / Audit 结论。
"""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ReportOutlineModel(Base):
    __tablename__ = "report_outlines"
    __table_args__ = (
        CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_report_outlines_research_question_sha256",
        ),
        CheckConstraint(
            f"outline_fingerprint {_SHA256_CHECK}",
            name="ck_report_outlines_fingerprint",
        ),
        CheckConstraint(
            "outline_schema_version >= 1",
            name="ck_report_outlines_schema_version",
        ),
        UniqueConstraint("outline_fingerprint", name="uq_report_outlines_fingerprint"),
        Index("ix_report_outlines_synthesis_result_id", "synthesis_result_id"),
        Index("ix_report_outlines_company_id", "company_id"),
        Index("ix_report_outlines_analysis_as_of", "analysis_as_of"),
    )

    outline_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    synthesis_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claim_synthesis_results.synthesis_result_id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_question_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    analysis_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    outline_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    outline_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    outline_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
