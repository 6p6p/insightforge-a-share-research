"""SQLAlchemy models for top-level research orchestration (stage 7A.2B.1).

`research_orchestration_runs` 是一次 **top-level orchestration** 的一等公民记录
（**不是 WorkflowRun**）：research task 从 Plan → Route → Prepare → Fulfill →
Stage4 child 的整个生命周期。它与 `workflow_runs` 完全分离——因此不与
`uq_workflow_runs_one_active_per_task` / `uq_workflow_runs_thread_id` 冲突。

- `input_fingerprint` CHAR(64) **UNIQUE**：schema + task_id + planner input
  fingerprint + orchestrator 身份的 SHA-256（spec F；不含 API key / created_at）。
  replay（同 task + 同 plan input + 同 orchestrator → 同一 orchestration）由该
  UNIQUE 保证；并发 create → 最终 1 行。
- **partial unique（task_id）WHERE active**：同 task 至多一个 active orchestration
  （pending/running/waiting_human）——与 workflow_runs 的 active invariant 并列，
  独立表、互不修改。
- `current_phase`：planning → routing → preparing → (fulfilling → preparing) →
  stage4 → awaiting_stage5（7A.2B.1 本轮正常 terminal phase；status 保持 running
  等 7A.2B.2 接 Stage5）→ … stage5 / research_backflow / completed（未来）。
- `research_plan_id` NULL-able：ensure_plan 之后才绑定（本轮 create_or_get 已建
  plan，实际总是非 NULL；保留 NULL 语义以覆盖恢复边界）。

`research_orchestration_child_runs` 是 orchestration → child WorkflowRun 的
**persisted ownership linkage**（spec D 最重要 correctness boundary）：

- **不得**用 `latest task + graph_name` 猜 child 归属；child lookup 必须精确
  `(orchestration_id, stage, attempt_no)` → exact `workflow_run_id`。
- `UNIQUE(workflow_run_id)`：一个 WorkflowRun 至多被一个 orchestration 拥有。
- `UNIQUE(orchestration_id, stage, attempt_no)`：同 orchestration 同 stage 同
  attempt 至多一个 child。
- `stage` v1 仅 `stage4`（`stage5` 未来）。backflow / continuation 用新的
  `attempt_no`，**不改写旧 link**（immutable history）。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

_ORCH_STATUS = "status IN ('pending','running','waiting_human','completed','failed','cancelled')"
_ORCH_PHASE = (
    "current_phase IN ('planning','routing','preparing','fulfilling','waiting_manual',"
    "'stage4','awaiting_stage5','stage5','research_backflow','completed')"
)
_CHILD_STAGE = "stage IN ('stage4','stage5')"


class ResearchOrchestrationModel(Base):
    __tablename__ = "research_orchestration_runs"
    __table_args__ = (
        CheckConstraint("orchestration_schema_version >= 1", name="ck_ro_schema_version"),
        CheckConstraint("orchestrator_version >= 1", name="ck_ro_orchestrator_version"),
        CheckConstraint("btrim(orchestrator_name) <> ''", name="ck_ro_orchestrator_name_not_blank"),
        CheckConstraint(_ORCH_STATUS, name="ck_ro_status"),
        CheckConstraint(_ORCH_PHASE, name="ck_ro_current_phase"),
        CheckConstraint(f"input_fingerprint {_SHA256_CHECK}", name="ck_ro_input_fingerprint"),
        UniqueConstraint("input_fingerprint", name="uq_research_orchestration_runs_input_fp"),
        # 同 task 至多一个 active orchestration（独立表，不修改 workflow_runs 的
        # uq_workflow_runs_one_active_per_task）。
        Index(
            "uq_research_orchestration_runs_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running', 'waiting_human')"),
        ),
        Index("ix_research_orchestration_runs_task_id", "task_id"),
        Index("ix_research_orchestration_runs_created_at", "created_at"),
    )

    orchestration_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
        nullable=False,
    )
    research_plan_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_plans.research_plan_id", ondelete="RESTRICT"),
        nullable=True,
    )
    orchestration_schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    orchestrator_name: Mapped[str] = mapped_column(String(64), nullable=False)
    orchestrator_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    current_phase: Mapped[str] = mapped_column(String(32), nullable=False)
    input_fingerprint: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )


class ResearchOrchestrationChildModel(Base):
    __tablename__ = "research_orchestration_child_runs"
    __table_args__ = (
        CheckConstraint("attempt_no >= 1", name="ck_ro_child_attempt_no"),
        CheckConstraint(_CHILD_STAGE, name="ck_ro_child_stage"),
        UniqueConstraint(
            "workflow_run_id",
            name="uq_research_orchestration_child_runs_workflow_run_id",
        ),
        UniqueConstraint(
            "orchestration_id",
            "stage",
            "attempt_no",
            name="uq_research_orchestration_child_runs_scope_attempt",
        ),
        Index("ix_research_orchestration_child_runs_orchestration_id", "orchestration_id"),
    )

    orchestration_child_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    orchestration_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_orchestration_runs.orchestration_id", ondelete="RESTRICT"),
        nullable=False,
    )
    workflow_run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"),
        nullable=False,
    )
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    source_research_request_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
