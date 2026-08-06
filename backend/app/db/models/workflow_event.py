"""SQLAlchemy model for workflow events."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    SmallInteger,
    String,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_EVENT_TYPE_CHECK = (
    "event_type IN ('run_created','run_started','node_completed','run_completed',"
    "'run_failed','run_waiting_human','run_resumed','run_cancelled')"
)


class WorkflowEventModel(Base):
    __tablename__ = "workflow_events"
    __table_args__ = (
        CheckConstraint(_EVENT_TYPE_CHECK, name="ck_workflow_events_type"),
        CheckConstraint(
            "progress IS NULL OR (progress BETWEEN 0 AND 100)",
            name="ck_workflow_events_progress",
        ),
        Index("ix_workflow_events_run_id", "run_id"),
        Index("ix_workflow_events_created_at", "created_at"),
        Index("ix_workflow_events_run_event", "run_id", "event_id"),
    )

    event_id: Mapped[int] = mapped_column(BigInteger, Identity(always=False), primary_key=True)
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    node_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    stage: Mapped[str | None] = mapped_column(String(64), nullable=True)
    progress: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    message: Mapped[str] = mapped_column(String(500), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
