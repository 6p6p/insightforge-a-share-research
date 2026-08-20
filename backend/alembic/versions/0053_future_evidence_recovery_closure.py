"""P0.5 research isolation closure: claim invalidation + bounded recovery counters.

- claims.invalidated_at / invalidation_reason：FutureEvidence 有界恢复把
  污染 claim 标记 invalid（**不删除**；synthesis 输入加载时排除）；
- research_orchestration_runs.future_evidence_recovery_attempts：每次
  FutureEvidence 恢复尝试 +1（bounded retry，防无限循环）。
"""

import sqlalchemy as sa

from alembic import op

revision: str = "0053"
down_revision: str | None = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "claims",
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "claims",
        sa.Column("invalidation_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "research_orchestration_runs",
        sa.Column(
            "future_evidence_recovery_attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )


def downgrade() -> None:
    op.drop_column("research_orchestration_runs", "future_evidence_recovery_attempts")
    op.drop_column("claims", "invalidation_reason")
    op.drop_column("claims", "invalidated_at")
