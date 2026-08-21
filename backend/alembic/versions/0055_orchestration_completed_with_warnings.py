"""v1.2.2 orchestration completed_with_warnings status.

人工批准带警告完成（degraded section 诚实占位已人工审核）——status 新增
`completed_with_warnings`（terminal，product 语义 = 研究完成且包含审核提醒）。
`current_phase` 不变（仍 `completed`），只放宽 `ck_ro_status` CheckConstraint。
"""

from alembic import op

revision: str = "0055"
down_revision: str | None = "0054"
branch_labels = None
depends_on = None

_OLD_STATUS = "status IN ('pending','running','waiting_human','completed','failed','cancelled')"
_NEW_STATUS = (
    "status IN ('pending','running','waiting_human','completed',"
    "'completed_with_warnings','failed','cancelled')"
)


def upgrade() -> None:
    op.drop_constraint("ck_ro_status", "research_orchestration_runs", type_="check")
    op.create_check_constraint("ck_ro_status", "research_orchestration_runs", _NEW_STATUS)


def downgrade() -> None:
    op.drop_constraint("ck_ro_status", "research_orchestration_runs", type_="check")
    op.create_check_constraint("ck_ro_status", "research_orchestration_runs", _OLD_STATUS)
