"""SQLAlchemy models for deterministic report assembly + check results (stage 5C).

`reports` 保存一次 **不可变、确定性装配**的报告：把一次已验证
`VerifiedReportOutline` + 显式选中的 `draft_sections`（每 Outline section 恰好
一个 DraftSection）机械地拼成完整报告正文，**不调用 LLM**（0 model identity）。

- outline_id FK `report_outlines.outline_id` RESTRICT、company_id FK
  `companies.company_id` RESTRICT——上游存在期间 Report 不静默消失；Report 不可变，
  无 update API（任意 DraftSection / Outline 变化 = 新 fingerprint = 新行）；
- research_question_sha256 / analysis_as_of 派生自 outline（verified artifacts），
  供检索；
- report_schema_version / report_fingerprint 由 ReportService 确定性派生；
  report_payload 为 JSONB（v1 = `{"sections":[{"section_id",...,"paragraphs":[...]}]}`，
  只存真实 Claim/Evidence UUID + conflict/gap indexes，不存 alias / prompt / raw
  provider response）；
- report_fingerprint UNIQUE：同 outline + 同 selected draft sections + 同 payload →
  replay 同一行；任一 DraftSection / Outline 变化 → 新指纹 → 新 Report（旧行保留）。

`report_check_results` 保存一次 **确定性报告检查**（10 个 v1 checks，0 LLM）的
结构化结果：status（pass/fail）+ 结构化 findings（不做长 prose）。check_fingerprint
UNIQUE：同 report + 同 schema + 同 findings → replay 同一行；Report 变化 →
report_fingerprint 不同 → 新 check_fingerprint → 新 CheckResult（旧行保留）。
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
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ReportModel(Base):
    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_reports_research_question_sha256",
        ),
        CheckConstraint(
            f"report_fingerprint {_SHA256_CHECK}",
            name="ck_reports_report_fingerprint",
        ),
        CheckConstraint(
            "report_schema_version >= 1",
            name="ck_reports_report_schema_version",
        ),
        UniqueConstraint("report_fingerprint", name="uq_reports_report_fingerprint"),
        Index("ix_reports_outline_id", "outline_id"),
        Index("ix_reports_company_id", "company_id"),
        Index("ix_reports_analysis_as_of", "analysis_as_of"),
    )

    report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    outline_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_outlines.outline_id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_question_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    analysis_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    report_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    report_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    report_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ReportCheckResultModel(Base):
    __tablename__ = "report_check_results"
    __table_args__ = (
        CheckConstraint(
            f"check_fingerprint {_SHA256_CHECK}",
            name="ck_report_check_results_check_fingerprint",
        ),
        CheckConstraint(
            "check_schema_version >= 1",
            name="ck_report_check_results_check_schema_version",
        ),
        CheckConstraint(
            "status IN ('pass','fail')",
            name="ck_report_check_results_status",
        ),
        UniqueConstraint(
            "check_fingerprint",
            name="uq_report_check_results_check_fingerprint",
        ),
        Index("ix_report_check_results_report_id", "report_id"),
    )

    check_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("reports.report_id", ondelete="RESTRICT"),
        nullable=False,
    )
    check_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    findings: Mapped[list] = mapped_column(JSONB, nullable=False)
    check_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
