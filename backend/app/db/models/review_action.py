"""SQLAlchemy models for report review routing + human confirmation (stage 5E.1).

`report_review_actions` 保存一次 **deterministic ReviewActionPlan**：从已验证的
`VerifiedReportAudit` 纯函数派生 action_type + action_payload，作为 5E.2 后续
rewrite / research / finalization 的稳定控制层输入。

- `audit_id` FK `report_audits.audit_id` RESTRICT、`report_id` FK
  `reports.report_id` RESTRICT——上游存在期间 Action 不静默消失；Action 不可变，
  无 update API（同一 immutable Audit 只能产生一个 deterministic Action，
  `(audit_id)` UNIQUE）；
- `action_type` VARCHAR(24)（finalize/rewrite/research/human_review）由程序根据
  audit status / recommended_route 确定性派生（spec F，调用方不得自选）；
- `action_payload` JSONB 全部从 Verified Audit + ReviewIssues 派生（source
  ids / target_section_ids / review_issue_ids / research 专用 related ids +
  research_need_codes），**不写长 prose**；
- `action_fingerprint` CHAR(64) UNIQUE = schema + audit_id + audit_fingerprint +
  report_id + report_fingerprint + action_type + normalized payload 的 SHA-256
  （**不含** review_action_id / created_at）。

`human_review_requests` 仅当 action_type=human_review 时创建（服务层保证）：
- `review_action_id` FK `report_review_actions.review_action_id` RESTRICT，
  `(review_action_id)` UNIQUE——一个 human_review action 至多一个 request；
- `request_payload` JSONB 只存 IDs + issue summaries（issue_type/severity/
  section_id/paragraph_index），**不复制** Evidence quote / 完整 paragraph /
  prompt（Web 后续按 IDs 再加载详情）；
- `request_fingerprint` CHAR(64) UNIQUE = schema + review_action_id +
  action_fingerprint + normalized payload 的 SHA-256。

`human_review_decisions` 一次人工裁决，一个 request 至多一个 immutable decision：
- `human_request_id` FK `human_review_requests.human_request_id` RESTRICT，
  `(human_request_id)` UNIQUE；
- `decision` VARCHAR(24)（approve/rewrite/research/cancel）+ 可选 `comment`
  （trim、<=1000 字符）+ `decided_at`（resolve 时写入）；
- `decision_fingerprint` CHAR(64) UNIQUE = schema + human_request_id +
  request_fingerprint + decision + normalized comment 的 SHA-256（**不含**
  human_decision_id / decided_at / created_at）。

人工 decision **不修改** Audit route / issues / Report（spec L，immutable artifact）。
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


class ReportReviewActionModel(Base):
    __tablename__ = "report_review_actions"
    __table_args__ = (
        CheckConstraint(
            "action_schema_version >= 1",
            name="ck_report_review_actions_action_schema_version",
        ),
        CheckConstraint(
            "action_type IN ('finalize','rewrite','research','human_review')",
            name="ck_report_review_actions_action_type",
        ),
        CheckConstraint(
            f"action_fingerprint {_SHA256_CHECK}",
            name="ck_report_review_actions_action_fingerprint",
        ),
        UniqueConstraint("audit_id", name="uq_report_review_actions_audit_id"),
        UniqueConstraint(
            "action_fingerprint",
            name="uq_report_review_actions_action_fingerprint",
        ),
        Index("ix_report_review_actions_report_id", "report_id"),
        Index("ix_report_review_actions_action_type", "action_type"),
    )

    review_action_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    audit_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_audits.audit_id", ondelete="RESTRICT"),
        nullable=False,
    )
    report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("reports.report_id", ondelete="RESTRICT"),
        nullable=False,
    )
    action_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action_type: Mapped[str] = mapped_column(String(24), nullable=False)
    action_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    action_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class HumanReviewRequestModel(Base):
    __tablename__ = "human_review_requests"
    __table_args__ = (
        CheckConstraint(
            "request_schema_version >= 1",
            name="ck_human_review_requests_request_schema_version",
        ),
        CheckConstraint(
            f"request_fingerprint {_SHA256_CHECK}",
            name="ck_human_review_requests_request_fingerprint",
        ),
        UniqueConstraint(
            "review_action_id",
            name="uq_human_review_requests_review_action_id",
        ),
        UniqueConstraint(
            "request_fingerprint",
            name="uq_human_review_requests_request_fingerprint",
        ),
    )

    human_request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    review_action_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_review_actions.review_action_id", ondelete="RESTRICT"),
        nullable=False,
    )
    request_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class HumanReviewDecisionModel(Base):
    __tablename__ = "human_review_decisions"
    __table_args__ = (
        CheckConstraint(
            "decision_schema_version >= 1",
            name="ck_human_review_decisions_decision_schema_version",
        ),
        CheckConstraint(
            "decision IN ('approve','rewrite','research','cancel')",
            name="ck_human_review_decisions_decision",
        ),
        CheckConstraint(
            f"decision_fingerprint {_SHA256_CHECK}",
            name="ck_human_review_decisions_decision_fingerprint",
        ),
        UniqueConstraint(
            "human_request_id",
            name="uq_human_review_decisions_human_request_id",
        ),
        UniqueConstraint(
            "decision_fingerprint",
            name="uq_human_review_decisions_decision_fingerprint",
        ),
    )

    human_decision_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    human_request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("human_review_requests.human_request_id", ondelete="RESTRICT"),
        nullable=False,
    )
    decision_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decision_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
