"""Empty baseline migration.

Revision ID: 0001
Revises:
Create Date: 2026-08-06

"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """No business tables yet; Alembic only records its version table."""
    pass


def downgrade() -> None:
    pass
