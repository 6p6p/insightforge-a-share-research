"""SQLAlchemy model for source records."""

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

_DOCUMENT_TYPE_CHECK = (
    "document_type IN ('annual_report','semiannual_report','quarterly_report',"
    "'company_announcement','issuer_ir_material','prospectus','news_article','other')"
)
_ACQUISITION_METHOD_CHECK = (
    "acquisition_method IN ('user_upload','user_provided_url','public_html',"
    "'automatic_discovery','user_supplied')"
)
_STATUS_CHECK = "status IN ('available')"


class SourceRecordModel(Base):
    __tablename__ = "source_records"
    __table_args__ = (
        CheckConstraint(
            _DOCUMENT_TYPE_CHECK,
            name="ck_source_records_document_type",
        ),
        CheckConstraint(
            _ACQUISITION_METHOD_CHECK,
            name="ck_source_records_acquisition_method",
        ),
        CheckConstraint(_STATUS_CHECK, name="ck_source_records_status"),
        CheckConstraint(
            "authority_tier_snapshot BETWEEN 1 AND 4",
            name="ck_source_records_authority_tier_snapshot",
        ),
        CheckConstraint(
            "jsonb_typeof(provider_capabilities_snapshot) = 'array'",
            name="ck_source_records_provider_capabilities_array",
        ),
        CheckConstraint(
            "btrim(title) <> ''",
            name="ck_source_records_title_not_blank",
        ),
        CheckConstraint(
            "source_url IS NULL OR source_url ~ '^https://'",
            name="ck_source_records_url_https",
        ),
        CheckConstraint(
            "source_url IS NULL OR source_url !~ '://[^/]*@'",
            name="ck_source_records_url_no_userinfo",
        ),
        CheckConstraint(
            "source_url IS NULL OR position('#' in source_url) = 0",
            name="ck_source_records_url_no_fragment",
        ),
        UniqueConstraint(
            "provider_key",
            "source_url",
            "artifact_id",
            name="uq_source_records_provider_url_artifact",
        ),
        Index("ix_source_records_company_id", "company_id"),
        Index("ix_source_records_provider_key", "provider_key"),
        Index("ix_source_records_artifact_id", "artifact_id"),
        Index("ix_source_records_published_at", "published_at"),
        Index("ix_source_records_document_type", "document_type"),
    )

    source_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    company_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("companies.company_id", ondelete="RESTRICT"),
        nullable=False,
    )
    provider_key: Mapped[str] = mapped_column(
        String(32),
        ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
        nullable=False,
    )
    artifact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_type: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reporting_period_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    acquisition_method: Mapped[str] = mapped_column(String(64), nullable=False)
    external_document_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    authority_tier_snapshot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    critical_claim_eligible_snapshot: Mapped[bool] = mapped_column(Boolean, nullable=False)
    # 登记时保存获取当时的 Provider 能力完整列表（稳定排序的字符串数组）。
    # 快照不随 Provider 后续策略修改而变化，保证历史来源记录可追溯。
    provider_capabilities_snapshot: Mapped[list] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, server_default=text("'available'")
    )
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
