"""research planning foundation

Revision ID: 0040
Revises: 0039
Create Date: 2026-08-12

阶段 7A.1：Research Planner + Deterministic Source Routing 持久化。

**`research_plans`**（spec G/H）：一次 **immutable** ResearchPlan——Planner 对某个
ResearchTask 的语义研究计划。

- `research_plan_id` UUID PK；
- `task_id` FK `research_tasks.task_id` RESTRICT、`company_id` FK
  `companies.company_id` RESTRICT——上游存在期间 Plan 不静默消失；
- `plan_schema_version` INTEGER >=1（payload 结构版本）；
- `planner_name` / `planner_version` / `model_id`——Planner 身份（进入 input
  fingerprint）；
- `planner_input_fingerprint` CHAR(64) **UNIQUE**：canonical planner 输入
  （schema + task_id + company identity snapshot + question + analysis_as_of +
  planner 身份 + strategy version）的 SHA-256。**不 UNIQUE(task_id)**——planner
  version / 输入变化后允许同一 task 产生新 immutable Plan；replay 由 input
  fingerprint 唯一性保证（同输入 → 同一行）；
- `plan_payload` JSONB（validated ResearchPlanPayload，对象）；
- `plan_fingerprint` CHAR(64) **UNIQUE** = input fingerprint + normalized
  validated payload（spec H）。tamper 由 `verify_research_plan_integrity`
  recompute 发现（不 repair）；
- `created_at` now()。

**`research_plan_routes`**（spec J/K）：当时 Router 对每个 need 的 deterministic
route decision——**保证 registry 变化后仍可审计当时的 route decision**（不为了少
一张表牺牲 provenance）。

- `route_plan_id` UUID PK；`research_plan_id` FK `research_plans.research_plan_id`
  RESTRICT；
- `route_schema_version` >=1；`router_name` / `router_version`（Router 身份）；
- `route_payload` JSONB（SourceRoutePlan v1，对象）；
- `route_fingerprint` CHAR(64) **UNIQUE** = plan fingerprint + router 身份 +
  normalized route payload；
- `UNIQUE(research_plan_id, router_version)`：同 plan 同 router version 至多一行
  （同 plan 重放 → 同一行）；
- `created_at` now()。

**downgrade guard**：两表任一存在任何行 → 拒绝回滚。Plan 与 Route 是正式 immutable
research planning artifact（即使可确定性重放，也不在 downgrade 时静默删除历史）；
alembic_version 保持 0040。两表皆空时才允许回到 0039。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0040"
down_revision: str | None = "0039"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLANS = "research_plans"
_ROUTES = "research_plan_routes"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def _create_research_plans() -> None:
    op.create_table(
        _PLANS,
        sa.Column("research_plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_schema_version", sa.Integer(), nullable=False),
        sa.Column("planner_name", sa.String(64), nullable=False),
        sa.Column("planner_version", sa.Integer(), nullable=False),
        sa.Column("model_id", sa.String(200), nullable=False),
        sa.Column(
            "planner_input_fingerprint",
            postgresql.CHAR(64),
            nullable=False,
        ),
        sa.Column("plan_payload", postgresql.JSONB(), nullable=False),
        sa.Column("plan_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("research_plan_id", name="pk_research_plans"),
        sa.CheckConstraint(
            f"planner_input_fingerprint {_SHA256_CHECK}",
            name="ck_research_plans_input_fingerprint",
        ),
        sa.CheckConstraint(
            f"plan_fingerprint {_SHA256_CHECK}",
            name="ck_research_plans_plan_fingerprint",
        ),
        sa.CheckConstraint(
            "plan_schema_version >= 1",
            name="ck_research_plans_plan_schema_version",
        ),
        sa.CheckConstraint(
            "planner_version >= 1",
            name="ck_research_plans_planner_version",
        ),
        sa.CheckConstraint(
            "btrim(planner_name) <> ''",
            name="ck_research_plans_planner_name_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(model_id) <> ''",
            name="ck_research_plans_model_id_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(plan_payload) = 'object'",
            name="ck_research_plans_plan_payload_object",
        ),
        sa.UniqueConstraint(
            "planner_input_fingerprint",
            name="uq_research_plans_planner_input_fingerprint",
        ),
        sa.UniqueConstraint(
            "plan_fingerprint",
            name="uq_research_plans_plan_fingerprint",
        ),
    )
    op.create_index(f"ix_{_PLANS}_task_id", _PLANS, ["task_id"])
    op.create_index(f"ix_{_PLANS}_company_id", _PLANS, ["company_id"])
    op.create_index(f"ix_{_PLANS}_created_at", _PLANS, ["created_at"])


def _create_research_plan_routes() -> None:
    op.create_table(
        _ROUTES,
        sa.Column("route_plan_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "research_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_plans.research_plan_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("route_schema_version", sa.Integer(), nullable=False),
        sa.Column("router_name", sa.String(64), nullable=False),
        sa.Column("router_version", sa.Integer(), nullable=False),
        sa.Column("route_payload", postgresql.JSONB(), nullable=False),
        sa.Column("route_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("route_plan_id", name="pk_research_plan_routes"),
        sa.CheckConstraint(
            f"route_fingerprint {_SHA256_CHECK}",
            name="ck_research_plan_routes_route_fingerprint",
        ),
        sa.CheckConstraint(
            "route_schema_version >= 1",
            name="ck_research_plan_routes_route_schema_version",
        ),
        sa.CheckConstraint(
            "router_version >= 1",
            name="ck_research_plan_routes_router_version",
        ),
        sa.CheckConstraint(
            "btrim(router_name) <> ''",
            name="ck_research_plan_routes_router_name_not_blank",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(route_payload) = 'object'",
            name="ck_research_plan_routes_route_payload_object",
        ),
        sa.UniqueConstraint(
            "route_fingerprint",
            name="uq_research_plan_routes_route_fingerprint",
        ),
        sa.UniqueConstraint(
            "research_plan_id",
            "router_version",
            name="uq_research_plan_routes_plan_router_version",
        ),
    )
    op.create_index(
        f"ix_{_ROUTES}_research_plan_id",
        _ROUTES,
        ["research_plan_id"],
    )
    op.create_index(f"ix_{_ROUTES}_created_at", _ROUTES, ["created_at"])


def upgrade() -> None:
    _create_research_plans()
    _create_research_plan_routes()


def downgrade() -> None:
    # 数据安全：Plan 与 Route 是正式 immutable research planning artifact，即使
    # 可确定性重放，也不在 downgrade 时静默删除历史。任一表有行 → 拒绝回滚
    # （不删除数据 / 不修改行），alembic_version 保持 0040。两表皆空时才允许
    # 回到 0039（先删 routes——FK 依赖 plans）。
    if _table_has_row(_PLANS) or _table_has_row(_ROUTES):
        raise RuntimeError(
            "cannot downgrade migration 0040: "
            f"rows present in {_PLANS} or {_ROUTES}; refusing to drop registered "
            "research plans / routes (alembic_version stays 0040)"
        )
    op.drop_table(_ROUTES)
    op.drop_table(_PLANS)
