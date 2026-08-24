"""v1.2.7-C archive tasks: add archived_at to research_tasks."""

import sqlalchemy as sa

from alembic import op

revision: str = "0057"
down_revision: str | None = "0056"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "research_tasks",
        sa.Column(
            "archived_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("research_tasks", "archived_at")
