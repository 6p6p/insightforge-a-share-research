"""SQLAlchemy model for macro dataset snapshots (stage 2C.2A).

一次"查询-获取"产生的不可变快照：保留查询请求、来源元数据、Provider 策略
快照与分页状态。snapshot_fingerprint 唯一标识一次获取（正式生成算法在
2C.2B 冻结，本阶段只冻结字段与唯一性，不在 Repository 内猜算法）。
"""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_FINGERPRINT_CHECK = "snapshot_fingerprint ~ '^[0-9a-f]{64}$'"
_REQUESTED_COUNTRY_CODE_CHECK = "requested_country_code ~ '^[A-Z]{2,3}$'"
_YEAR_RANGE_CHECK = "query_start_year >= 1960 AND query_start_year <= query_end_year"
_YEAR_SPAN_CHECK = "query_end_year - query_start_year + 1 <= 60"
_SOURCE_ID_SNAPSHOT_CHECK = "btrim(source_id_snapshot) <> ''"
_ISO3_FORMAT_CHECK = "provider_country_id ~ '^[A-Z]{3}$' AND iso3_code ~ '^[A-Z]{3}$'"
_ISO2_FORMAT_CHECK = "iso2_code ~ '^[A-Z]{2}$'"
_INDICATOR_NAME_CHECK = "btrim(indicator_name) <> ''"
_SOURCE_NAME_CHECK = "btrim(source_name) <> ''"
_GEOGRAPHY_NAME_CHECK = "btrim(geography_name) <> ''"
_PAGE_CHECK = "page >= 1"
_PAGES_CHECK = "pages >= 1"
_PAGE_LE_PAGES_CHECK = "page <= pages"
_PER_PAGE_CHECK = "per_page >= 1"
_PROVIDER_TOTAL_CHECK = "provider_total >= 0"
_REQUEST_COUNT_CHECK = "request_count >= 1 AND request_count <= 20"
_ACQUISITION_METHOD_CHECK = "acquisition_method = 'official_api'"
_AUTHORITY_TIER_CHECK = "authority_tier_snapshot BETWEEN 1 AND 4"
_TOPICS_ARRAY_CHECK = "jsonb_typeof(topics_snapshot) = 'array'"
_CAPABILITIES_ARRAY_CHECK = "jsonb_typeof(provider_capabilities_snapshot) = 'array'"
_STATUS_CHECK = "status = 'available'"


class MacroDatasetSnapshotModel(Base):
    __tablename__ = "macro_dataset_snapshots"
    __table_args__ = (
        CheckConstraint(_FINGERPRINT_CHECK, name="ck_macro_dataset_snapshots_fingerprint"),
        CheckConstraint(
            _REQUESTED_COUNTRY_CODE_CHECK,
            name="ck_macro_dataset_snapshots_requested_country_code",
        ),
        CheckConstraint(_YEAR_RANGE_CHECK, name="ck_macro_dataset_snapshots_year_range"),
        CheckConstraint(_YEAR_SPAN_CHECK, name="ck_macro_dataset_snapshots_year_span"),
        CheckConstraint(
            _SOURCE_ID_SNAPSHOT_CHECK,
            name="ck_macro_dataset_snapshots_source_id_not_blank",
        ),
        CheckConstraint(_ISO3_FORMAT_CHECK, name="ck_macro_dataset_snapshots_iso3_format"),
        CheckConstraint(_ISO2_FORMAT_CHECK, name="ck_macro_dataset_snapshots_iso2_format"),
        CheckConstraint(
            _INDICATOR_NAME_CHECK,
            name="ck_macro_dataset_snapshots_indicator_name_not_blank",
        ),
        CheckConstraint(
            _SOURCE_NAME_CHECK,
            name="ck_macro_dataset_snapshots_source_name_not_blank",
        ),
        CheckConstraint(
            _GEOGRAPHY_NAME_CHECK,
            name="ck_macro_dataset_snapshots_geography_name_not_blank",
        ),
        CheckConstraint(_PAGE_CHECK, name="ck_macro_dataset_snapshots_page"),
        CheckConstraint(_PAGES_CHECK, name="ck_macro_dataset_snapshots_pages"),
        CheckConstraint(_PAGE_LE_PAGES_CHECK, name="ck_macro_dataset_snapshots_page_le_pages"),
        CheckConstraint(_PER_PAGE_CHECK, name="ck_macro_dataset_snapshots_per_page"),
        CheckConstraint(_PROVIDER_TOTAL_CHECK, name="ck_macro_dataset_snapshots_provider_total"),
        CheckConstraint(_REQUEST_COUNT_CHECK, name="ck_macro_dataset_snapshots_request_count"),
        CheckConstraint(
            _ACQUISITION_METHOD_CHECK,
            name="ck_macro_dataset_snapshots_acquisition_method",
        ),
        CheckConstraint(
            _AUTHORITY_TIER_CHECK,
            name="ck_macro_dataset_snapshots_authority_tier_snapshot",
        ),
        CheckConstraint(_TOPICS_ARRAY_CHECK, name="ck_macro_dataset_snapshots_topics_array"),
        CheckConstraint(
            _CAPABILITIES_ARRAY_CHECK,
            name="ck_macro_dataset_snapshots_provider_capabilities_array",
        ),
        CheckConstraint(_STATUS_CHECK, name="ck_macro_dataset_snapshots_status"),
        UniqueConstraint(
            "snapshot_fingerprint",
            name="uq_macro_dataset_snapshots_fingerprint",
        ),
        Index("ix_macro_dataset_snapshots_series_id", "series_id"),
        Index("ix_macro_dataset_snapshots_fetched_at", text("fetched_at DESC")),
        Index("ix_macro_dataset_snapshots_requested_country_code", "requested_country_code"),
    )

    snapshot_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    series_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("macro_series.series_id", ondelete="RESTRICT"),
        nullable=False,
    )
    snapshot_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_country_code: Mapped[str] = mapped_column(String(3), nullable=False)
    query_start_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    query_end_year: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source_id_snapshot: Mapped[str] = mapped_column(String(32), nullable=False)
    indicator_name: Mapped[str] = mapped_column(String(500), nullable=False)
    indicator_unit: Mapped[str] = mapped_column(String(100), nullable=False)
    source_name: Mapped[str] = mapped_column(String(500), nullable=False)
    source_note: Mapped[str] = mapped_column(Text, nullable=False)
    source_organization: Mapped[str] = mapped_column(Text, nullable=False)
    topics_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    provider_country_id: Mapped[str] = mapped_column(String(3), nullable=False)
    iso2_code: Mapped[str] = mapped_column(String(2), nullable=False)
    iso3_code: Mapped[str] = mapped_column(String(3), nullable=False)
    geography_name: Mapped[str] = mapped_column(String(300), nullable=False)
    region_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    income_level_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    page: Mapped[int] = mapped_column(Integer, nullable=False)
    pages: Mapped[int] = mapped_column(Integer, nullable=False)
    per_page: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_total: Mapped[int] = mapped_column(Integer, nullable=False)
    provider_last_updated: Mapped[str | None] = mapped_column(String(64), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    request_count: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    acquisition_method: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_tier_snapshot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    critical_claim_eligible_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    provider_capabilities_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'available'")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
