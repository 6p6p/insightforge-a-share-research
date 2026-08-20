"""P1 stage5 degraded-section closure: draft_sections.status + degraded_reason.

degraded section 保留完整 DraftSection contract（status=degraded + degraded_reason），
assembler 因此始终拿到 S1..S6 的 DraftSection 行；degraded 正文是确定性诚实说明
（无 claim/数字/引文）。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0054"
down_revision: str | None = "0053"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "draft_sections",
        sa.Column("status", sa.String(16), nullable=False, server_default="completed"),
    )
    op.add_column(
        "draft_sections",
        sa.Column("degraded_reason", sa.Text(), nullable=True),
    )
    op.create_check_constraint(
        "ck_draft_sections_status_valid",
        "draft_sections",
        "status IN ('completed', 'degraded')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_draft_sections_status_valid", "draft_sections", type_="check")
    op.drop_column("draft_sections", "degraded_reason")
    op.drop_column("draft_sections", "status")
