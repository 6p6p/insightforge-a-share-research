"""SQLAlchemy model for company identities."""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    CHAR,
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

_EXCHANGE_CHECK = "exchange IN ('SSE','SZSE','BSE')"
_BOARD_CHECK = "board IN ('sse_main','star','szse_main','chinext','bse')"
_LISTING_STATUS_CHECK = "listing_status IN ('listed','delisted','unknown')"
_EXCHANGE_BOARD_CHECK = (
    "(exchange = 'SSE' AND board IN ('sse_main','star')) OR "
    "(exchange = 'SZSE' AND board IN ('szse_main','chinext')) OR "
    "(exchange = 'BSE' AND board = 'bse')"
)


class CompanyModel(Base):
    __tablename__ = "companies"
    __table_args__ = (
        CheckConstraint(_EXCHANGE_CHECK, name="ck_companies_exchange"),
        CheckConstraint(
            "security_code ~ '^[0-9]{6}$'",
            name="ck_companies_security_code",
        ),
        CheckConstraint(
            "identity_key = exchange || ':' || security_code",
            name="ck_companies_identity_key",
        ),
        CheckConstraint(_BOARD_CHECK, name="ck_companies_board"),
        CheckConstraint(
            _LISTING_STATUS_CHECK,
            name="ck_companies_listing_status",
        ),
        CheckConstraint(
            _EXCHANGE_BOARD_CHECK,
            name="ck_companies_exchange_board_consistency",
        ),
        CheckConstraint(
            "delisting_date IS NULL OR delisting_date >= listing_date",
            name="ck_companies_dates",
        ),
        UniqueConstraint("identity_key", name="uq_companies_identity_key"),
        UniqueConstraint(
            "exchange",
            "security_code",
            name="uq_companies_exchange_code",
        ),
        Index("ix_companies_security_code", "security_code"),
        Index("ix_companies_official_name", "official_name"),
        Index("ix_companies_short_name", "short_name"),
    )

    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    exchange: Mapped[str] = mapped_column(String(16), nullable=False)
    security_code: Mapped[str] = mapped_column(CHAR(6), nullable=False)
    identity_key: Mapped[str] = mapped_column(String(32), nullable=False)
    board: Mapped[str] = mapped_column(String(32), nullable=False)
    official_name: Mapped[str] = mapped_column(String(200), nullable=False)
    short_name: Mapped[str] = mapped_column(String(100), nullable=False)
    listing_status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'unknown'")
    )
    listing_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    delisting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    identity_source_provider_key: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_providers.provider_key"),
        nullable=False,
    )
    identity_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    source_updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
