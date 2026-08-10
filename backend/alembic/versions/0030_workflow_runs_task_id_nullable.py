"""workflow_runs.task_id nullable (Stage 4 has no research_task)

Revision ID: 0030
Revises: 0029
Create Date: 2026-08-10

阶段 4D.2：Stage 4 LangGraph 分析工作流。

`workflow_runs.task_id` 原为 NOT NULL FK `research_tasks.task_id`（Stage 1
simulation 强制绑定一个 research_task）。Stage 4 分析工作流由 API 直接驱动
（company_id + research_question + analysis_as_of + analysis_work_items），
**没有** research_task，因此必须放宽为 nullable：

1. `task_id` → nullable。partial unique index `uq_workflow_runs_one_active_per_task`
   保持原样——NULL 不参与唯一约束，多个无 task 的 Stage 4 活跃 run 不会冲突；
2. Stage 1 的 research_task 绑定 run 不受影响（task_id 仍填值）；
3. `downgrade` guard：存在任何 `task_id IS NULL` 的 run 时拒绝回滚（恢复 NOT
   NULL 会静默破坏 Stage 4 run），**不删除任何数据**。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0030"
down_revision: str | None = "0029"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workflow_runs"


def upgrade() -> None:
    op.alter_column(
        _TABLE,
        "task_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )


def downgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {_TABLE} WHERE task_id IS NULL LIMIT 1"))
    if rows.first() is not None:
        raise RuntimeError(
            "cannot downgrade migration 0030: stage 4 workflow runs present; "
            "refusing to restore NOT NULL silently"
        )
    op.alter_column(
        _TABLE,
        "task_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )
