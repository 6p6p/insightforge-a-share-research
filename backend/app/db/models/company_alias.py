"""SQLAlchemy model for company aliases."""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_ALIAS_TYPE_CHECK = "alias_type IN ('official_name','short_name','former_name','english_name')"


class CompanyAliasModel(Base):
    __tablename__ = "company_aliases"
    __table_args__ = (
        CheckConstraint(_ALIAS_TYPE_CHECK, name="ck_company_aliases_type"),
        CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_company_aliases_dates",
        ),
        UniqueConstraint(
            "company_id",
            "normalized_alias",
            "alias_type",
            name="uq_company_aliases_company_alias_type",
        ),
        Index("ix_company_aliases_normalized_alias", "normalized_alias"),
    )

    alias_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="CASCADE"),
        nullable=False,
    )
    alias: Mapped[str] = mapped_column(String(200), nullable=False)
    normalized_alias: Mapped[str] = mapped_column(String(200), nullable=False)
    alias_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_provider_key: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_providers.provider_key"),
        nullable=False,
    )
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
