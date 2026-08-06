"""SQLAlchemy model for research tasks."""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Index,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_STATUS_CHECK = (
    "status IN ('pending','running','waiting_human','retrying','completed','failed','cancelled')"
)
_STAGE_CHECK = (
    "current_stage IN ('created','planning','collecting','parsing','evidence_extraction',"
    "'analyzing','synthesizing','writing','checking','auditing','exporting')"
)


class ResearchTaskModel(Base):
    __tablename__ = "research_tasks"
    __table_args__ = (
        CheckConstraint(
            "research_start_date <= research_end_date",
            name="ck_research_tasks_date_range",
        ),
        CheckConstraint(
            "progress BETWEEN 0 AND 100",
            name="ck_research_tasks_progress_range",
        ),
        CheckConstraint(_STATUS_CHECK, name="ck_research_tasks_status"),
        CheckConstraint(_STAGE_CHECK, name="ck_research_tasks_stage"),
        CheckConstraint(
            "(idempotency_key IS NULL AND request_fingerprint IS NULL) OR "
            "(idempotency_key IS NOT NULL AND request_fingerprint IS NOT NULL)",
            name="ck_research_tasks_idempotency_pair",
        ),
        UniqueConstraint("idempotency_key", name="uq_research_tasks_idempotency_key"),
        Index("ix_research_tasks_status", "status"),
        Index("ix_research_tasks_created_at", "created_at"),
    )

    task_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    company_query: Mapped[str] = mapped_column(String(100), nullable=False)
    research_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    research_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    modules: Mapped[list] = mapped_column(JSONB, nullable=False)
    questions: Mapped[list] = mapped_column(JSONB, nullable=False)
    include_relative_valuation: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    require_plan_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true")
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'pending'")
    )
    current_stage: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default=text("'created'")
    )
    progress: Mapped[int] = mapped_column(SmallInteger, nullable=False, server_default=text("0"))
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(CHAR(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
