"""SQLAlchemy model for workflow runs."""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_STATUS_CHECK = "status IN ('pending','running','waiting_human','completed','failed','cancelled')"


class WorkflowRunModel(Base):
    __tablename__ = "workflow_runs"
    __table_args__ = (
        CheckConstraint(_STATUS_CHECK, name="ck_workflow_runs_status"),
        CheckConstraint(
            "(status = 'waiting_human' AND pending_action IS NOT NULL) OR "
            "(status <> 'waiting_human' AND pending_action IS NULL)",
            name="ck_workflow_runs_pending_action_consistency",
        ),
        UniqueConstraint("thread_id", name="uq_workflow_runs_thread_id"),
        Index("ix_workflow_runs_task_id", "task_id"),
        Index("ix_workflow_runs_status", "status"),
        Index("ix_workflow_runs_created_at", "created_at"),
        Index(
            "uq_workflow_runs_one_active_per_task",
            "task_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running', 'waiting_human')"),
        ),
    )

    run_id: Mapped[UUID] = mapped_column(PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id: Mapped[UUID | None] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
        nullable=True,
    )
    thread_id: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_name: Mapped[str] = mapped_column(String(64), nullable=False)
    graph_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    pending_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
