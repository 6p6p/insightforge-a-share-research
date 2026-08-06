"""workflow run invariants

Revision ID: 0006
Revises: 0005
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index("uq_workflow_runs_one_active_per_task", table_name="workflow_runs")
    op.create_index(
        "uq_workflow_runs_one_active_per_task",
        "workflow_runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running', 'waiting_human')"),
    )
    op.create_check_constraint(
        "ck_workflow_runs_pending_action_consistency",
        "workflow_runs",
        "(status = 'waiting_human' AND pending_action IS NOT NULL) OR "
        "(status <> 'waiting_human' AND pending_action IS NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_workflow_runs_pending_action_consistency",
        "workflow_runs",
        type_="check",
    )
    op.drop_index("uq_workflow_runs_one_active_per_task", table_name="workflow_runs")
    op.create_index(
        "uq_workflow_runs_one_active_per_task",
        "workflow_runs",
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
