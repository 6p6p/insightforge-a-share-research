"""research orchestration retry schema

Revision ID: 0043
Revises: 0042
Create Date: 2026-08-12

阶段 7A.2B.2：真正实现 user retry（spec B/C/D）。

**为什么**：0042 用 `UNIQUE(input_fingerprint)` 做 replay 唯一性，但同 ResearchPlan
的 user retry 必须生成 **NEW orchestration_id + NEW top-level thread**，即使 research
input 完全相同（spec B）——fingerprint 不变，但要有第二个 orchestration。因此
fingerprint 不再是唯一键。

`research_orchestration_runs` 变更：

- `attempt_no INTEGER NOT NULL`（现有 rows 回填 `1`；CHECK `attempt_no >= 1`）；
- `retry_of_orchestration_id UUID NULL`（FK `research_orchestration_runs`
  RESTRICT；CHECK 不能指向自己）；由 service integrity 验证 retry_of 必须同
  task/plan（不做复杂 trigger）；
- `input_fingerprint` 保持 64-char SHA-256（仍表示 task + planner input +
  orchestrator identity，spec C），但 **移除 UNIQUE**；
- 新增 `UNIQUE(research_plan_id, attempt_no)`：同 plan 的 attempt 1/2/3 允许
  并存历史（各占唯一 attempt），replay / 并发 retry 由此保证。

**downgrade guard**：`research_orchestration_runs` 存在任何 row → 拒绝回滚。
0042 依赖 `UNIQUE(input_fingerprint)`，retry rows（同 fingerprint 多行）无法安全
表示；alembic_version 保持 0043。空表时才允许回到 0042。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0043"
down_revision: str | None = "0042"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORCH = "research_orchestration_runs"

# 与 model `ck_ro_input_fingerprint` 一致（fingerprint 仍要求 SHA-256 格式）。
_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    # attempt_no：现有 rows 回填 1，然后去掉 server_default（未来显式提供）。
    op.add_column(
        _ORCH,
        sa.Column("attempt_no", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.alter_column(_ORCH, "attempt_no", server_default=None)
    op.create_check_constraint("ck_ro_attempt_no", _ORCH, "attempt_no >= 1")

    # retry_of_orchestration_id（FK RESTRICT + 不自指 CHECK）。
    op.add_column(
        _ORCH,
        sa.Column(
            "retry_of_orchestration_id",
            postgresql.UUID(as_uuid=True),
            nullable=True,
        ),
    )
    op.create_foreign_key(
        "fk_research_orchestration_runs_retry_of",
        _ORCH,
        _ORCH,
        ["retry_of_orchestration_id"],
        ["orchestration_id"],
        ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_ro_retry_of_not_self",
        _ORCH,
        "retry_of_orchestration_id IS NULL OR retry_of_orchestration_id <> orchestration_id",
    )

    # input_fingerprint 不再 UNIQUE（fingerprint 可对应 attempt 1/2/3）。
    op.drop_constraint("uq_research_orchestration_runs_input_fp", _ORCH, type_="unique")

    # 唯一性改由 (research_plan_id, attempt_no) 承担。
    op.create_unique_constraint(
        "uq_research_orchestration_runs_plan_attempt",
        _ORCH,
        ["research_plan_id", "attempt_no"],
    )


def downgrade() -> None:
    # 数据安全：Orchestration 是正式 immutable artifact；0042 需要恢复
    # UNIQUE(input_fingerprint)，retry rows（同 fingerprint 多行）无法安全表示。
    # 有行 → 拒绝回滚（不删除数据 / 不修改行），alembic_version 保持 0043。
    if _table_has_row(_ORCH):
        raise RuntimeError(
            "cannot downgrade migration 0043: rows present in "
            f"{_ORCH}; refusing to drop retryable research orchestration runs "
            "(alembic_version stays 0043)"
        )
    op.drop_constraint("uq_research_orchestration_runs_plan_attempt", _ORCH, type_="unique")
    op.create_unique_constraint(
        "uq_research_orchestration_runs_input_fp", _ORCH, ["input_fingerprint"]
    )
    op.drop_constraint("ck_ro_retry_of_not_self", _ORCH, type_="check")
    op.drop_constraint("fk_research_orchestration_runs_retry_of", _ORCH, type_="foreignkey")
    op.drop_column(_ORCH, "retry_of_orchestration_id")
    op.drop_constraint("ck_ro_attempt_no", _ORCH, type_="check")
    op.drop_column(_ORCH, "attempt_no")
