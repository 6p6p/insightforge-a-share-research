"""SQLAlchemy model for company master snapshot provenance records."""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CompanyMasterSnapshotRow(Base):
    __tablename__ = "company_master_snapshots"
    __table_args__ = (
        CheckConstraint(
            "company_count >= 0 AND alias_count >= 0",
            name="ck_company_master_snapshots_counts",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_company_master_snapshots_sha256",
        ),
        UniqueConstraint(
            "snapshot_version",
            "content_sha256",
            name="uq_company_master_snapshots_version_sha",
        ),
        Index("ix_company_master_snapshots_version", "snapshot_version"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_version: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(CHAR(64), nullable=False)
    company_count: Mapped[int] = mapped_column(Integer, nullable=False)
    alias_count: Mapped[int] = mapped_column(Integer, nullable=False)
    sources: Mapped[dict] = mapped_column(JSONB, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
