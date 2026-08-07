"""SQLAlchemy model for macro series identity (stage 2C.2A)."""

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

# 只保存稳定身份：provider_key/source_id/external_indicator_id/geography_type/
# geography_code/frequency 六字段；名称/单位/地区名/收入水平等可变属性存到
# MacroDatasetSnapshot，不放在 Series。
_SOURCE_ID_CHECK = "btrim(source_id) <> ''"
_EXTERNAL_INDICATOR_ID_CHECK = "external_indicator_id ~ '^[A-Z0-9._-]{1,64}$'"
_GEOGRAPHY_TYPE_CHECK = "geography_type IN ('country')"
_GEOGRAPHY_CODE_CHECK = "geography_code ~ '^[A-Z]{3}$'"
_FREQUENCY_CHECK = "frequency IN ('annual')"
_PROVIDER_KEY_CHECK = "btrim(provider_key) <> ''"


class MacroSeriesModel(Base):
    __tablename__ = "macro_series"
    __table_args__ = (
        CheckConstraint(_SOURCE_ID_CHECK, name="ck_macro_series_source_id_not_blank"),
        CheckConstraint(
            _EXTERNAL_INDICATOR_ID_CHECK,
            name="ck_macro_series_external_indicator_id_format",
        ),
        CheckConstraint(_GEOGRAPHY_TYPE_CHECK, name="ck_macro_series_geography_type"),
        CheckConstraint(_GEOGRAPHY_CODE_CHECK, name="ck_macro_series_geography_code"),
        CheckConstraint(_FREQUENCY_CHECK, name="ck_macro_series_frequency"),
        CheckConstraint(_PROVIDER_KEY_CHECK, name="ck_macro_series_provider_key_not_blank"),
        UniqueConstraint(
            "provider_key",
            "source_id",
            "external_indicator_id",
            "geography_type",
            "geography_code",
            "frequency",
            name="uq_macro_series_identity",
        ),
        Index("ix_macro_series_provider_key", "provider_key"),
        Index("ix_macro_series_external_indicator_id", "external_indicator_id"),
        Index("ix_macro_series_geography_code", "geography_code"),
        Index("ix_macro_series_frequency", "frequency"),
    )

    series_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    provider_key: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
        nullable=False,
    )
    source_id: Mapped[str] = mapped_column(String(32), nullable=False)
    external_indicator_id: Mapped[str] = mapped_column(String(64), nullable=False)
    geography_type: Mapped[str] = mapped_column(String(32), nullable=False)
    geography_code: Mapped[str] = mapped_column(String(16), nullable=False)
    frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
