"""restore research_task linkage on workflow_runs (Stage 4 runs belong to a task)

Revision ID: 0031
Revises: 0030
Create Date: 2026-08-10

阶段 4 Final Gate：恢复 ResearchTask → WorkflowRun 关系。

0030 曾把 `workflow_runs.task_id` 放宽为 nullable（当时 Stage 4 分析工作流假设
无 research_task）——这是**错误的最终语义**。Stage 4 WorkflowRun 仍必须属于一个
ResearchTask；Stage 4 分析由 API 以 `task_id` 驱动，run 记录必须绑定到任务。

upgrade：
1. **拒绝升级**：存在任何 `task_id IS NULL` 的 run → 明确拒绝（**不**猜 task、
   **不**自动创建 fake ResearchTask、**不**自动绑定）；调用方应先清理测试数据；
2. `task_id` 恢复 NOT NULL。

downgrade：回到 nullable（0030 语义），不删除任何数据。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0031"
down_revision: str | None = "0030"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "workflow_runs"


def upgrade() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {_TABLE} WHERE task_id IS NULL LIMIT 1"))
    if rows.first() is not None:
        raise RuntimeError(
            "cannot upgrade migration 0031: workflow runs without a research_task exist; "
            "refusing to restore NOT NULL silently (no task guessing / auto-binding)"
        )
    op.alter_column(
        _TABLE,
        "task_id",
        existing_type=sa.Uuid(),
        nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        _TABLE,
        "task_id",
        existing_type=sa.Uuid(),
        nullable=True,
    )
