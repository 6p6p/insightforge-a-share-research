"""planner fallback telemetry columns

Revision ID: 0051
Revises: 0050
Create Date: 2026-08-17

Add planner_fallback_used and planner_repair_attempts columns to
research_plans for planner reliability telemetry (P1: Planner must not
be a single point of failure). These are internal-only fields; never
exposed to users.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0051"
down_revision: str | None = "0050"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "research_plans",
        sa.Column(
            "planner_fallback_used",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "research_plans",
        sa.Column(
            "planner_repair_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("research_plans", "planner_repair_attempts")
    op.drop_column("research_plans", "planner_fallback_used")
