"""backflow human review closure tables

Revision ID: 0052
Revises: 0051
Create Date: 2026-02-18

P0 research_backflow_limit_reached -> Human Review closed loop:

- backflow_human_review_requests: at most one persistence-backed review request
  per orchestration (created idempotently when the research_backflow_manual
  terminal is reached). Equivalent persistent object; never mutates only the
  orchestration status.
- backflow_human_review_decisions: immutable human adjudication (accept /
  extra_research / cancel) per request.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0052"
down_revision: str | None = "0051"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA = "~ '^[0-9a-f]{64}$'"


def upgrade() -> None:
    op.create_table(
        "backflow_human_review_requests",
        sa.Column("backflow_human_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("orchestration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reason", sa.String(length=64), nullable=False),
        sa.Column("request_schema_version", sa.Integer(), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("request_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["orchestration_id"],
            ["research_orchestration_runs.orchestration_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("backflow_human_request_id"),
        sa.CheckConstraint(
            "request_schema_version >= 1",
            name="ck_backflow_human_review_requests_request_schema_version",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_backflow_human_review_requests_reason_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(request_payload) = 'object'",
            name="ck_backflow_human_review_requests_payload_object",
        ),
        sa.CheckConstraint(
            f"request_fingerprint {_SHA}",
            name="ck_backflow_human_review_requests_request_fingerprint",
        ),
        sa.UniqueConstraint(
            "orchestration_id",
            name="uq_backflow_human_review_requests_orchestration_id",
        ),
    )
    op.create_table(
        "backflow_human_review_decisions",
        sa.Column("backflow_human_decision_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("backflow_human_request_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("decision_schema_version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(length=24), nullable=False),
        sa.Column("comment", sa.String(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_fingerprint", sa.CHAR(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["backflow_human_request_id"],
            ["backflow_human_review_requests.backflow_human_request_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("backflow_human_decision_id"),
        sa.CheckConstraint(
            "decision_schema_version >= 1",
            name="ck_backflow_human_review_decisions_decision_schema_version",
        ),
        sa.CheckConstraint(
            "decision IN ('accept','extra_research','cancel')",
            name="ck_backflow_human_review_decisions_decision",
        ),
        sa.CheckConstraint(
            f"decision_fingerprint {_SHA}",
            name="ck_backflow_human_review_decisions_decision_fingerprint",
        ),
        sa.UniqueConstraint(
            "backflow_human_request_id",
            name="uq_backflow_human_review_decisions_backflow_human_request_id",
        ),
    )


def downgrade() -> None:
    op.drop_table("backflow_human_review_decisions")
    op.drop_table("backflow_human_review_requests")
