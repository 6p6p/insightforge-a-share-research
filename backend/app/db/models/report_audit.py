"""SQLAlchemy models for evidence-bound report audit (stage 5D).

`report_audits` 保存一次 **Evidence-bound 语义审计** 的聚合记录（0..50 个
ReviewIssue，Agent 只判断"语义上是否真的成立"，不重算数字 / 不重写正文）：

- report_id FK `reports.report_id` RESTRICT、check_result_id FK
  `report_check_results.check_result_id` RESTRICT——上游存在期间 Audit 不静默
  消失；Audit 不可变，无 update API（report / check / pack 任一变化 =
  新 audit_input_fingerprint = 新行）；
- audit_schema_version / auditor_name / auditor_version / auditor_model_id 由
  Audit 服务确定性派生（production = deepseek-v4-flash，thinking disabled，
  temperature=0，structured output，0 tools / 0 web）；
- audit_input_fingerprint CHAR(64) **UNIQUE**：audit schema + report / check
  指纹 + auditor 身份 + normalized pack 身份（section/paragraph 结构 +
  Claim/Evidence 指纹 + ClaimEvidence relation + synthesis conflict/gap 身份）。
  调用 LLM 前按它 replay——命中 → 0 model calls；同 input → replay 同一行
  （UNIQUE 是并发唯一性来源，audit_fingerprint 不 UNIQUE）；
- audit_status（pass/fail）/ recommended_route（pass/rewrite/research/
  human_review）由程序根据 resolved issues 确定性派生（spec O，模型不决定
  routing）；audit_fingerprint = input 指纹 + normalized issues + status +
  route 的 SHA-256（NOT UNIQUE）。

`review_issues` 保存具体 issue 明细：ordinal 为 normalize 后的 deterministic
序号，`(audit_id, ordinal)` UNIQUE；resolved UUID 列表（related_claim_ids /
related_evidence_card_ids）存 JSONB，**不建 link table**（spec G），replay
严格验证。
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
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


class ReportAuditModel(Base):
    __tablename__ = "report_audits"
    __table_args__ = (
        CheckConstraint(
            "audit_schema_version >= 1",
            name="ck_report_audits_audit_schema_version",
        ),
        CheckConstraint(
            "auditor_version >= 1",
            name="ck_report_audits_auditor_version",
        ),
        CheckConstraint(
            "issue_count >= 0",
            name="ck_report_audits_issue_count",
        ),
        CheckConstraint(
            "audit_status IN ('pass','fail')",
            name="ck_report_audits_audit_status",
        ),
        CheckConstraint(
            "recommended_route IN ('pass','rewrite','research','human_review')",
            name="ck_report_audits_recommended_route",
        ),
        CheckConstraint(
            f"audit_input_fingerprint {_SHA256_CHECK}",
            name="ck_report_audits_audit_input_fingerprint",
        ),
        CheckConstraint(
            f"audit_fingerprint {_SHA256_CHECK}",
            name="ck_report_audits_audit_fingerprint",
        ),
        UniqueConstraint(
            "audit_input_fingerprint",
            name="uq_report_audits_audit_input_fingerprint",
        ),
        Index("ix_report_audits_report_id", "report_id"),
        Index("ix_report_audits_check_result_id", "check_result_id"),
        Index("ix_report_audits_audit_fingerprint", "audit_fingerprint"),
    )

    audit_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("reports.report_id", ondelete="RESTRICT"),
        nullable=False,
    )
    check_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_check_results.check_result_id", ondelete="RESTRICT"),
        nullable=False,
    )
    audit_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    auditor_name: Mapped[str] = mapped_column(String, nullable=False)
    auditor_version: Mapped[int] = mapped_column(Integer, nullable=False)
    auditor_model_id: Mapped[str] = mapped_column(String, nullable=False)
    audit_input_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    audit_status: Mapped[str] = mapped_column(String(16), nullable=False)
    recommended_route: Mapped[str] = mapped_column(String(24), nullable=False)
    issue_count: Mapped[int] = mapped_column(Integer, nullable=False)
    audit_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ReviewIssueModel(Base):
    __tablename__ = "review_issues"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 1",
            name="ck_review_issues_ordinal",
        ),
        CheckConstraint(
            "paragraph_index IS NULL OR paragraph_index >= 0",
            name="ck_review_issues_paragraph_index",
        ),
        CheckConstraint(
            "severity IN ('normal','high','critical')",
            name="ck_review_issues_severity",
        ),
        UniqueConstraint(
            "audit_id",
            "ordinal",
            name="uq_review_issues_audit_id_ordinal",
        ),
        Index("ix_review_issues_audit_id", "audit_id"),
    )

    review_issue_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    audit_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_audits.audit_id", ondelete="CASCADE"),
        nullable=False,
    )
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    issue_type: Mapped[str] = mapped_column(String(40), nullable=False)
    severity: Mapped[str] = mapped_column(String(16), nullable=False)
    section_id: Mapped[str] = mapped_column(String, nullable=False)
    paragraph_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    related_claim_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    related_evidence_card_ids: Mapped[list] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
