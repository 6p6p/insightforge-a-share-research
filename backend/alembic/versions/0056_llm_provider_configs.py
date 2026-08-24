"""v1.2.7-B user-configured LLM provider configs table."""

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0056"
down_revision: str | None = "0055"
branch_labels = None
depends_on = None

_NOT_NULL_FALSE = sa.text("false")
_NOW = sa.text("now()")


def upgrade() -> None:
    op.create_table(
        "llm_provider_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("provider", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=120), nullable=False),
        sa.Column("model_id", sa.String(length=160), nullable=False),
        sa.Column("base_url", sa.String(length=255), nullable=True),
        sa.Column("encrypted_api_key", sa.String(length=512), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=False,
            server_default=_NOT_NULL_FALSE,
        ),
        sa.Column(
            "has_api_key",
            sa.Boolean(),
            nullable=False,
            server_default=_NOT_NULL_FALSE,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=_NOW,
        ),
    )


def downgrade() -> None:
    op.drop_table("llm_provider_configs")
