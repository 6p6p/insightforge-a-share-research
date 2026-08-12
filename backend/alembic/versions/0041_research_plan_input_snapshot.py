"""research plan planner input snapshot

Revision ID: 0041
Revises: 0040
Create Date: 2026-08-12

阶段 7A.1 Final Gate（spec A）：冻结 **creation-time PlannerInputSnapshot**。

给 `research_plans` 增加两列，持久化 create_plan 那一刻的 planner 输入：

- `planner_input_payload` JSONB NULL：`ResearchPlannerInputSnapshot`（task_id /
  company_id / 公司语义身份 / aliases 稳定排序 / research_question / analysis_as_of）。
  只存语义输入；**不存** API key / prompt / model response / DB metadata。
- `planner_input_schema_version` INTEGER NULL：snapshot schema 版本（当前 v1）。

**conditional CHECK**：v2 行（`plan_schema_version >= 2`）必须同时携带
`planner_input_payload` 与 `planner_input_schema_version >= 1`；v1 legacy 行允许
NULL（不删除 / 不 rewrite 旧行，不回填）。payload 若是 JSON 必须是 object。

verify 的 v2 路径只重放 stored snapshot 重建 input fingerprint——公司别名 /
short_name 等 master-data 正常演化不再误判成 tamper（spec A）。

**downgrade guard**：存在任何 v2 input snapshot 行（`planner_input_payload IS NOT
NULL`）→ 拒绝回滚（不删除 / 不改写数据，alembic_version 保持 0041）。仅当所有行
都无 input snapshot（空表或纯 v1 行）时允许删列——此时两列全 NULL，删除无数据损失。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLANS = "research_plans"

_CHECK_V2 = "ck_research_plans_v2_input_snapshot"
_CHECK_PAYLOAD_OBJECT = "ck_research_plans_planner_input_payload_object"


def _table_has_v2_input_snapshot() -> bool:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            f"SELECT 1 FROM {_PLANS} "
            "WHERE planner_input_payload IS NOT NULL "
            "OR planner_input_schema_version IS NOT NULL LIMIT 1"
        )
    )
    return rows.first() is not None


def upgrade() -> None:
    op.add_column(
        _PLANS,
        sa.Column("planner_input_payload", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        _PLANS,
        sa.Column("planner_input_schema_version", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        _CHECK_PAYLOAD_OBJECT,
        _PLANS,
        "(planner_input_payload IS NULL) OR (jsonb_typeof(planner_input_payload) = 'object')",
    )
    op.create_check_constraint(
        _CHECK_V2,
        _PLANS,
        "NOT (plan_schema_version >= 2) OR "
        "(planner_input_payload IS NOT NULL AND planner_input_schema_version >= 1)",
    )


def downgrade() -> None:
    # 数据安全：PlannerInputSnapshot 是正式 immutable research planning artifact，
    # 不在 downgrade 时静默删除。存在 v2 snapshot 行 → 拒绝回滚（不删除 / 不改写
    # 行），alembic_version 保持 0041。仅当无任何 snapshot 行（空表或纯 v1 行，两列
    # 全 NULL）时才允许删列——删除无数据损失。
    if _table_has_v2_input_snapshot():
        raise RuntimeError(
            "cannot downgrade migration 0041: planner input snapshot rows present; "
            "refusing to drop frozen planner input payloads (alembic_version stays 0041)"
        )
    op.drop_constraint(_CHECK_V2, _PLANS, type_="check")
    op.drop_constraint(_CHECK_PAYLOAD_OBJECT, _PLANS, type_="check")
    op.drop_column(_PLANS, "planner_input_schema_version")
    op.drop_column(_PLANS, "planner_input_payload")
