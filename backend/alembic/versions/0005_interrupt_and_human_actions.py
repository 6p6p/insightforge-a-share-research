"""interrupt and human actions

Revision ID: 0005
Revises: 0004
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUN_STATUS_CHECK = (
    "status IN ('pending','running','waiting_human','completed','failed','cancelled')"
)
_EVENT_TYPE_CHECK = (
    "event_type IN ('run_created','run_started','node_completed','run_completed',"
    "'run_failed','run_waiting_human','run_resumed','run_cancelled')"
)
_ACTION_TYPE_CHECK = "action_type IN ('approve_plan')"


def upgrade() -> None:
    # workflow_runs：扩展 status CHECK + 增加 pending_action
    op.drop_constraint("ck_workflow_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint("ck_workflow_runs_status", "workflow_runs", _RUN_STATUS_CHECK)
    op.add_column(
        "workflow_runs",
        sa.Column("pending_action", sa.String(length=64), nullable=True),
    )

    # workflow_events：扩展 event_type CHECK
    op.drop_constraint("ck_workflow_events_type", "workflow_events", type_="check")
    op.create_check_constraint("ck_workflow_events_type", "workflow_events", _EVENT_TYPE_CHECK)

    # human_actions
    op.create_table(
        "human_actions",
        sa.Column("action_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("interrupt_key", sa.String(length=64), nullable=False),
        sa.Column("action_type", sa.String(length=64), nullable=False),
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
        sa.CheckConstraint(_ACTION_TYPE_CHECK, name="ck_human_actions_type"),
        sa.UniqueConstraint(
            "run_id",
            "interrupt_key",
            name="uq_human_actions_run_interrupt",
        ),
    )
    op.create_index("ix_human_actions_run_id", "human_actions", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_human_actions_run_id", table_name="human_actions")
    op.drop_table("human_actions")

    op.drop_constraint("ck_workflow_events_type", "workflow_events", type_="check")
    op.create_check_constraint(
        "ck_workflow_events_type",
        "workflow_events",
        "event_type IN ('run_created','run_started','node_completed','run_completed','run_failed')",
    )

    op.drop_column("workflow_runs", "pending_action")
    op.drop_constraint("ck_workflow_runs_status", "workflow_runs", type_="check")
    op.create_check_constraint(
        "ck_workflow_runs_status",
        "workflow_runs",
        "status IN ('pending','running','completed','failed','cancelled')",
    )
