"""create workflow_events table

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EVENT_TYPE_CHECK = (
    "event_type IN ('run_created','run_started','node_completed','run_completed','run_failed')"
)


def upgrade() -> None:
    op.create_table(
        "workflow_events",
        sa.Column("event_id", sa.BigInteger(), sa.Identity(always=False), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("node_name", sa.String(length=64), nullable=True),
        sa.Column("stage", sa.String(length=64), nullable=True),
        sa.Column("progress", sa.SmallInteger(), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _EVENT_TYPE_CHECK,
            name="ck_workflow_events_type",
        ),
        sa.CheckConstraint(
            "progress IS NULL OR (progress BETWEEN 0 AND 100)",
            name="ck_workflow_events_progress",
        ),
    )
    op.create_index("ix_workflow_events_run_id", "workflow_events", ["run_id"])
    op.create_index("ix_workflow_events_created_at", "workflow_events", ["created_at"])
    op.create_index("ix_workflow_events_run_event", "workflow_events", ["run_id", "event_id"])


def downgrade() -> None:
    op.drop_index("ix_workflow_events_run_event", table_name="workflow_events")
    op.drop_index("ix_workflow_events_created_at", table_name="workflow_events")
    op.drop_index("ix_workflow_events_run_id", table_name="workflow_events")
    op.drop_table("workflow_events")
