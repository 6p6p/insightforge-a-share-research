"""SQLAlchemy model for human actions on workflow runs."""

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_ACTION_TYPE_CHECK = "action_type IN ('approve_plan')"


class HumanActionModel(Base):
    __tablename__ = "human_actions"
    __table_args__ = (
        CheckConstraint(_ACTION_TYPE_CHECK, name="ck_human_actions_type"),
        UniqueConstraint(
            "run_id",
            "interrupt_key",
            name="uq_human_actions_run_interrupt",
        ),
        Index("ix_human_actions_run_id", "run_id"),
    )

    action_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    run_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("workflow_runs.run_id", ondelete="CASCADE"),
        nullable=False,
    )
    interrupt_key: Mapped[str] = mapped_column(String(64), nullable=False)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
