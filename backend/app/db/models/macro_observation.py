"""SQLAlchemy model for macro observations (stage 2C.2A).

单条年度观测绑定 MacroDatasetSnapshot。值使用 PostgreSQL NUMERIC（不用
DOUBLE PRECISION/REAL/FLOAT），保证超 2^53 的人口等大数全程十进制精确；
value_numeric IS NULL 当且仅当 is_missing=true。
"""

import uuid
from datetime import date, datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_PERIOD_CHECK = "period ~ '^[0-9]{4}$'"
_NORMALIZED_DATE_CHECK = (
    "extract(year from normalized_period_start) = period::integer AND "
    "extract(month from normalized_period_start) = 1 AND "
    "extract(day from normalized_period_start) = 1"
)
_PERIOD_SEMANTICS_CHECK = "period_semantics = 'provider_year_label'"
_FREQUENCY_CHECK = "frequency = 'annual'"
_VALUE_IS_MISSING_CHECK = (
    "(value_numeric IS NULL AND is_missing = true) OR "
    "(value_numeric IS NOT NULL AND is_missing = false)"
)
_MISSING_SCALE_CHECK = "(is_missing = true AND decimal_scale IS NULL) OR is_missing = false"
_PRESENT_SCALE_CHECK = (
    "(is_missing = false AND decimal_scale IS NOT NULL AND decimal_scale >= 0) OR is_missing = true"
)


class MacroObservationModel(Base):
    __tablename__ = "macro_observations"
    __table_args__ = (
        CheckConstraint(_PERIOD_CHECK, name="ck_macro_observations_period"),
        CheckConstraint(
            _NORMALIZED_DATE_CHECK,
            name="ck_macro_observations_normalized_period_start",
        ),
        CheckConstraint(_PERIOD_SEMANTICS_CHECK, name="ck_macro_observations_period_semantics"),
        CheckConstraint(_FREQUENCY_CHECK, name="ck_macro_observations_frequency"),
        CheckConstraint(_VALUE_IS_MISSING_CHECK, name="ck_macro_observations_value_is_missing"),
        CheckConstraint(_MISSING_SCALE_CHECK, name="ck_macro_observations_missing_scale"),
        CheckConstraint(_PRESENT_SCALE_CHECK, name="ck_macro_observations_present_scale"),
        UniqueConstraint("snapshot_id", "period", name="uq_macro_observations_snapshot_period"),
        Index("ix_macro_observations_snapshot_id", "snapshot_id"),
        Index("ix_macro_observations_normalized_period_start", "normalized_period_start"),
        Index("ix_macro_observations_period", "period"),
    )

    observation_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    snapshot_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_dataset_snapshots.snapshot_id", ondelete="CASCADE"),
        nullable=False,
    )
    period: Mapped[str] = mapped_column(String(4), nullable=False)
    normalized_period_start: Mapped[date] = mapped_column(Date, nullable=False)
    period_semantics: Mapped[str] = mapped_column(String(64), nullable=False)
    frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    value_numeric: Mapped[object | None] = mapped_column(Numeric, nullable=True)
    is_missing: Mapped[bool] = mapped_column(Boolean, nullable=False)
    observation_status: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decimal_scale: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
