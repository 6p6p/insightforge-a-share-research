"""SQLAlchemy model for source providers."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

_PROVIDER_TYPE_CHECK = (
    "provider_type IN ('exchange','regulator','statutory_disclosure_platform',"
    "'issuer','government_data','authoritative_data','international_organization',"
    "'professional_media','general_web','media')"
)


class SourceProviderModel(Base):
    __tablename__ = "source_providers"
    __table_args__ = (
        CheckConstraint(
            "authority_tier BETWEEN 1 AND 4",
            name="ck_source_providers_authority_tier",
        ),
        CheckConstraint(_PROVIDER_TYPE_CHECK, name="ck_source_providers_type"),
        CheckConstraint(
            "provider_key ~ '^[a-z0-9_-]+$'",
            name="ck_source_providers_key_format",
        ),
        CheckConstraint(
            "homepage_url !~ '://[^/]*@'",
            name="ck_source_providers_homepage_no_userinfo",
        ),
        CheckConstraint(
            "jsonb_typeof(allowed_domains) = 'array'",
            name="ck_source_providers_allowed_domains_array",
        ),
        CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name="ck_source_providers_capabilities_array",
        ),
        CheckConstraint(
            "jsonb_typeof(acquisition_methods) = 'array'",
            name="ck_source_providers_acquisition_methods_array",
        ),
        CheckConstraint(
            "jsonb_typeof(exchange_scope) = 'array'",
            name="ck_source_providers_exchange_scope_array",
        ),
        Index("ix_source_providers_authority_tier", "authority_tier"),
        Index("ix_source_providers_enabled", "enabled"),
    )

    provider_key: Mapped[str] = mapped_column(String(32), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(64), nullable=False)
    authority_tier: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    homepage_url: Mapped[str] = mapped_column(Text, nullable=False)
    allowed_domains: Mapped[list] = mapped_column(JSONB, nullable=False)
    capabilities: Mapped[list] = mapped_column(JSONB, nullable=False)
    acquisition_methods: Mapped[list] = mapped_column(JSONB, nullable=False)
    exchange_scope: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    requires_api_key: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    critical_claim_eligible: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
