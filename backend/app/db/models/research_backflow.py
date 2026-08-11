"""SQLAlchemy models for research backflow contract (stage 5E.2B).

`research_backflow_requests` 保存一次 **research_required** Stage 5 run 的可验证
研究交接请求（immutable）：source Stage 5 run + 裁决后的 review action（± human
decision）+ source Report → 确定性身份 / cutoff 绑定 + structured 交接 payload。

- `source_stage5_run_id` FK `workflow_runs.run_id` RESTRICT、`(source_stage5_run_id)`
  UNIQUE——一个 Stage 5 run 至多一个 research 请求（服务层 create_or_get replay）；
- `review_action_id` FK `report_review_actions.review_action_id` RESTRICT NOT NULL
  （legal trigger 只能是 research action 或 human_review action，服务层保证）；
- `human_decision_id` FK `human_review_decisions.human_decision_id` RESTRICT NULL
  （direct research 为 NULL，human research 非空）；
- `source_report_id` FK `reports.report_id` RESTRICT NOT NULL——身份 / cutoff 从
  Report → Outline → Synthesis chain 恢复（caller 不能提供）；
- `company_id` FK `companies.company_id` RESTRICT NOT NULL、
  `research_question_sha256` CHAR(64) NOT NULL、`analysis_as_of` DATE NOT NULL；
- `request_payload` JSONB 结构化交接（review_issue_ids / target_section_ids /
  related_claim_ids / related_evidence_card_ids / research_need_codes），**不写长
  prose / 不自动生成 query**；
- `request_fingerprint` CHAR(64) UNIQUE = schema + source run + action（± decision）
  + report + 身份/cutoff + normalized payload 的 SHA-256（**不含**
  research_request_id / created_at）。

`research_backflow_fulfillments` 保存 upstream（Stage 2/3/4）返回新 SynthesisResult
后的 fulfillment（immutable）：
- `research_request_id` FK RESTRICT、`(research_request_id)` UNIQUE——一个请求至多
  一个 fulfillment（不同结果不覆盖 → `ResearchBackflowAlreadyFulfilled`）；
- `new_synthesis_result_id` FK `claim_synthesis_results.synthesis_result_id`
  RESTRICT NOT NULL（同一 company / research-question / cutoff，且不是 source
  synthesis——no-progress 政策服务层保证）；
- `fulfillment_fingerprint` CHAR(64) UNIQUE = schema + request id+fingerprint +
  new synthesis result id+result fingerprint + new synthesis run id+synthesis
  fingerprint（**不含** fulfillment_id / created_at）。
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


class ResearchBackflowRequestModel(Base):
    __tablename__ = "research_backflow_requests"
    __table_args__ = (
        CheckConstraint(
            "request_schema_version >= 1",
            name="ck_research_backflow_requests_request_schema_version",
        ),
        CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_research_backflow_requests_research_question_sha256",
        ),
        CheckConstraint(
            f"request_fingerprint {_SHA256_CHECK}",
            name="ck_research_backflow_requests_request_fingerprint",
        ),
        UniqueConstraint(
            "source_stage5_run_id",
            name="uq_research_backflow_requests_source_stage5_run_id",
        ),
        UniqueConstraint(
            "request_fingerprint",
            name="uq_research_backflow_requests_request_fingerprint",
        ),
        Index("ix_research_backflow_requests_review_action_id", "review_action_id"),
        Index("ix_research_backflow_requests_human_decision_id", "human_decision_id"),
        Index("ix_research_backflow_requests_source_report_id", "source_report_id"),
        Index("ix_research_backflow_requests_company_id", "company_id"),
    )

    research_request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    source_stage5_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    review_action_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("report_review_actions.review_action_id", ondelete="RESTRICT"),
        nullable=False,
    )
    human_decision_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("human_review_decisions.human_decision_id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_report_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("reports.report_id", ondelete="RESTRICT"),
        nullable=False,
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_question_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    analysis_as_of: Mapped[date] = mapped_column(Date, nullable=False)
    request_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    request_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ResearchBackflowFulfillmentModel(Base):
    __tablename__ = "research_backflow_fulfillments"
    __table_args__ = (
        CheckConstraint(
            "fulfillment_schema_version >= 1",
            name="ck_research_backflow_fulfillments_fulfillment_schema_version",
        ),
        CheckConstraint(
            f"fulfillment_fingerprint {_SHA256_CHECK}",
            name="ck_research_backflow_fulfillments_fulfillment_fingerprint",
        ),
        UniqueConstraint(
            "research_request_id",
            name="uq_research_backflow_fulfillments_research_request_id",
        ),
        UniqueConstraint(
            "fulfillment_fingerprint",
            name="uq_research_backflow_fulfillments_fulfillment_fingerprint",
        ),
        Index(
            "ix_research_backflow_fulfillments_new_synthesis_result_id",
            "new_synthesis_result_id",
        ),
    )

    fulfillment_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    research_request_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_backflow_requests.research_request_id", ondelete="RESTRICT"),
        nullable=False,
    )
    new_synthesis_result_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("claim_synthesis_results.synthesis_result_id", ondelete="RESTRICT"),
        nullable=False,
    )
    fulfillment_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    fulfillment_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
