"""research orchestration foundation

Revision ID: 0042
Revises: 0041
Create Date: 2026-08-12

阶段 7A.2B.1：Top-level Research Orchestration 一等公民持久化。

**`research_orchestration_runs`**（spec B）：一次 **top-level orchestration**——
research task 从 Plan → Route → Prepare → Fulfill → Stage4 child 的生命周期。
**不是 WorkflowRun**：与 `workflow_runs` 完全分离，不与
`uq_workflow_runs_one_active_per_task` / `uq_workflow_runs_thread_id` 冲突。

- `orchestration_id` UUID PK；`task_id` FK `research_tasks` RESTRICT；
  `research_plan_id` FK `research_plans` RESTRICT NULL（ensure_plan 后绑定）；
- `orchestration_schema_version` >=1；`orchestrator_name` / `orchestrator_version`
  （orchestrator 身份，进入 input fingerprint）；
- `status`（pending/running/waiting_human/completed/failed/cancelled）与
  `current_phase`（planning/routing/preparing/fulfilling/waiting_manual/stage4/
  awaiting_stage5/stage5/research_backflow/completed）——**不修改 workflow_runs
  的状态语义**；
- `input_fingerprint` CHAR(64) **UNIQUE** = schema + task_id + planner input
  fingerprint + orchestrator 身份（spec F）。replay 由该 UNIQUE 保证；**不含**
  API key / created_at；
- **partial unique（task_id）WHERE status IN (pending/running/waiting_human)**：
  同 task 至多一个 active orchestration（独立表）。
- `started_at` / `completed_at` / `error_code` / `error_message` / `created_at` /
  `updated_at`。

**`research_orchestration_child_runs`**（spec C/D）：orchestration → child
WorkflowRun 的 **persisted ownership linkage**。

- `orchestration_child_id` UUID PK；`orchestration_id` FK
  `research_orchestration_runs` RESTRICT；`workflow_run_id` FK `workflow_runs`
  RESTRICT；`stage`（stage4/stage5）；`attempt_no` >=1；
  `source_research_request_id` NULL（未来 backflow continuation）；
- `UNIQUE(workflow_run_id)`：一个 WorkflowRun 至多被一个 orchestration 拥有；
- `UNIQUE(orchestration_id, stage, attempt_no)`：同 orchestration 同 stage 同
  attempt 至多一个 child；
- child lookup **必须精确** `(orchestration_id, stage, attempt_no)` → exact
  `workflow_run_id`，不得用 `latest task + graph_name` 猜归属。

**downgrade guard**：两张表任一存在任何行 → 拒绝回滚。Orchestration 是正式
immutable artifact（即使可确定性 replay，也不在 downgrade 时静默删除历史）；
alembic_version 保持 0042。两表皆空时才允许回到 0041（先删 child——FK 依赖
orchestration）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0042"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORCH = "research_orchestration_runs"
_CHILD = "research_orchestration_child_runs"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

_ORCH_STATUS = "status IN ('pending','running','waiting_human','completed','failed','cancelled')"
_ORCH_PHASE = (
    "current_phase IN ('planning','routing','preparing','fulfilling','waiting_manual',"
    "'stage4','awaiting_stage5','stage5','research_backflow','completed')"
)
_CHILD_STAGE = "stage IN ('stage4','stage5')"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def _create_research_orchestration_runs() -> None:
    op.create_table(
        _ORCH,
        sa.Column("orchestration_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "research_plan_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_plans.research_plan_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("orchestration_schema_version", sa.Integer(), nullable=False),
        sa.Column("orchestrator_name", sa.String(64), nullable=False),
        sa.Column("orchestrator_version", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            sa.String(32),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("current_phase", sa.String(32), nullable=False),
        sa.Column("input_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(500), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("orchestration_id", name="pk_research_orchestration_runs"),
        sa.CheckConstraint(
            "orchestration_schema_version >= 1",
            name="ck_ro_schema_version",
        ),
        sa.CheckConstraint("orchestrator_version >= 1", name="ck_ro_orchestrator_version"),
        sa.CheckConstraint(
            "btrim(orchestrator_name) <> ''",
            name="ck_ro_orchestrator_name_not_blank",
        ),
        sa.CheckConstraint(_ORCH_STATUS, name="ck_ro_status"),
        sa.CheckConstraint(_ORCH_PHASE, name="ck_ro_current_phase"),
        sa.CheckConstraint(f"input_fingerprint {_SHA256_CHECK}", name="ck_ro_input_fingerprint"),
        sa.UniqueConstraint(
            "input_fingerprint",
            name="uq_research_orchestration_runs_input_fp",
        ),
    )
    op.create_index(
        "uq_research_orchestration_runs_one_active_per_task",
        _ORCH,
        ["task_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running', 'waiting_human')"),
    )
    op.create_index(f"ix_{_ORCH}_task_id", _ORCH, ["task_id"])
    op.create_index(f"ix_{_ORCH}_created_at", _ORCH, ["created_at"])


def _create_research_orchestration_child_runs() -> None:
    op.create_table(
        _CHILD,
        sa.Column("orchestration_child_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "orchestration_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_orchestration_runs.orchestration_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "workflow_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("source_research_request_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "orchestration_child_id",
            name="pk_research_orchestration_child_runs",
        ),
        sa.CheckConstraint("attempt_no >= 1", name="ck_ro_child_attempt_no"),
        sa.CheckConstraint(_CHILD_STAGE, name="ck_ro_child_stage"),
        sa.UniqueConstraint(
            "workflow_run_id",
            name="uq_research_orchestration_child_runs_workflow_run_id",
        ),
        sa.UniqueConstraint(
            "orchestration_id",
            "stage",
            "attempt_no",
            name="uq_research_orchestration_child_runs_scope_attempt",
        ),
    )
    op.create_index(
        f"ix_{_CHILD}_orchestration_id",
        _CHILD,
        ["orchestration_id"],
    )


def upgrade() -> None:
    _create_research_orchestration_runs()
    _create_research_orchestration_child_runs()


def downgrade() -> None:
    # 数据安全：Orchestration 是正式 immutable artifact，不在 downgrade 时静默
    # 删除。任一表有行 → 拒绝回滚（不删除数据 / 不修改行），alembic_version 保持
    # 0042。两表皆空时才允许回到 0041（先删 child——FK 依赖 orchestration）。
    if _table_has_row(_ORCH) or _table_has_row(_CHILD):
        raise RuntimeError(
            "cannot downgrade migration 0042: rows present in "
            f"{_ORCH} or {_CHILD}; refusing to drop registered research "
            "orchestration runs / child links (alembic_version stays 0042)"
        )
    op.drop_table(_CHILD)
    op.drop_table(_ORCH)
