"""create research_tasks table

Revision ID: 0002
Revises: 0001
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_STATUS_CHECK = (
    "status IN ('pending','running','waiting_human','retrying','completed','failed','cancelled')"
)
_STAGE_CHECK = (
    "current_stage IN ('created','planning','collecting','parsing','evidence_extraction',"
    "'analyzing','synthesizing','writing','checking','auditing','exporting')"
)


def upgrade() -> None:
    op.create_table(
        "research_tasks",
        sa.Column("task_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("company_query", sa.String(length=100), nullable=False),
        sa.Column("research_start_date", sa.Date(), nullable=False),
        sa.Column("research_end_date", sa.Date(), nullable=False),
        sa.Column("modules", postgresql.JSONB(), nullable=False),
        sa.Column("questions", postgresql.JSONB(), nullable=False),
        sa.Column(
            "include_relative_valuation",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "require_plan_approval",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'pending'"),
            nullable=False,
        ),
        sa.Column(
            "current_stage",
            sa.String(length=64),
            server_default=sa.text("'created'"),
            nullable=False,
        ),
        sa.Column(
            "progress",
            sa.SmallInteger(),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column("idempotency_key", sa.String(length=128), nullable=True),
        sa.Column("request_fingerprint", sa.CHAR(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "research_start_date <= research_end_date",
            name="ck_research_tasks_date_range",
        ),
        sa.CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name="ck_research_tasks_progress_range",
        ),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_research_tasks_status"),
        sa.CheckConstraint(_STAGE_CHECK, name="ck_research_tasks_stage"),
        sa.CheckConstraint(
            "(idempotency_key IS NULL AND request_fingerprint IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)",
            name="ck_research_tasks_idempotency_pair",
        ),
        sa.UniqueConstraint("idempotency_key", name="uq_research_tasks_idempotency_key"),
    )
    op.create_index("ix_research_tasks_status", "research_tasks", ["status"])
    op.create_index("ix_research_tasks_created_at", "research_tasks", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_research_tasks_created_at", table_name="research_tasks")
    op.drop_index("ix_research_tasks_status", table_name="research_tasks")
    op.drop_table("research_tasks")
