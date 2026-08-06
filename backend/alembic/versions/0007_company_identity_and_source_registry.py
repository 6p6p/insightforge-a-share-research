"""company identity and source registry

Revision ID: 0007
Revises: 0006
Create Date: 2026-08-06

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_TYPE_CHECK = (
    "provider_type IN ('exchange','regulator','statutory_disclosure_platform',"
    "'issuer','government_data','authoritative_data','international_organization',"
    "'professional_media','general_web')"
)
_EXCHANGE_CHECK = "exchange IN ('SSE','SZSE','BSE')"
_BOARD_CHECK = "board IN ('sse_main','star','szse_main','chinext','bse')"
_LISTING_STATUS_CHECK = "listing_status IN ('listed','delisted','unknown')"
_EXCHANGE_BOARD_CHECK = (
    "(exchange = 'SSE' AND board IN ('sse_main','star')) OR "
    "(exchange = 'SZSE' AND board IN ('szse_main','chinext')) OR "
    "(exchange = 'BSE' AND board = 'bse')"
)
_ALIAS_TYPE_CHECK = "alias_type IN ('official_name','short_name','former_name','english_name')"


def upgrade() -> None:
    op.create_table(
        "source_providers",
        sa.Column("provider_key", sa.String(length=32), primary_key=True),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("provider_type", sa.String(length=64), nullable=False),
        sa.Column("authority_tier", sa.SmallInteger(), nullable=False),
        sa.Column("homepage_url", sa.Text(), nullable=False),
        sa.Column("allowed_domains", postgresql.JSONB(), nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("acquisition_methods", postgresql.JSONB(), nullable=False),
        sa.Column(
            "exchange_scope",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "requires_api_key",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "critical_claim_eligible",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "authority_tier BETWEEN 1 AND 4",
            name="ck_source_providers_authority_tier",
        ),
        sa.CheckConstraint(_PROVIDER_TYPE_CHECK, name="ck_source_providers_type"),
        sa.CheckConstraint(
            "provider_key ~ '^[a-z0-9_-]+$'",
            name="ck_source_providers_key_format",
        ),
        sa.CheckConstraint(
            "homepage_url !~ '://[^/]*@'",
            name="ck_source_providers_homepage_no_userinfo",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(allowed_domains) = 'array'",
            name="ck_source_providers_allowed_domains_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(capabilities) = 'array'",
            name="ck_source_providers_capabilities_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(acquisition_methods) = 'array'",
            name="ck_source_providers_acquisition_methods_array",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(exchange_scope) = 'array'",
            name="ck_source_providers_exchange_scope_array",
        ),
    )
    op.create_index(
        "ix_source_providers_authority_tier",
        "source_providers",
        ["authority_tier"],
    )
    op.create_index("ix_source_providers_enabled", "source_providers", ["enabled"])

    op.create_table(
        "companies",
        sa.Column("company_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("exchange", sa.String(length=16), nullable=False),
        sa.Column("security_code", sa.CHAR(length=6), nullable=False),
        sa.Column("identity_key", sa.String(length=32), nullable=False),
        sa.Column("board", sa.String(length=32), nullable=False),
        sa.Column("official_name", sa.String(length=200), nullable=False),
        sa.Column("short_name", sa.String(length=100), nullable=False),
        sa.Column(
            "listing_status",
            sa.String(length=32),
            server_default=sa.text("'unknown'"),
            nullable=False,
        ),
        sa.Column("listing_date", sa.Date(), nullable=True),
        sa.Column("delisting_date", sa.Date(), nullable=True),
        sa.Column(
            "identity_source_provider_key",
            sa.String(length=32),
            sa.ForeignKey("source_providers.provider_key"),
            nullable=False,
        ),
        sa.Column("identity_source_url", sa.Text(), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_EXCHANGE_CHECK, name="ck_companies_exchange"),
        sa.CheckConstraint(
            "security_code ~ '^[0-9]{6}$'",
            name="ck_companies_security_code",
        ),
        sa.CheckConstraint(
            "identity_key = exchange || ':' || security_code",
            name="ck_companies_identity_key",
        ),
        sa.CheckConstraint(_BOARD_CHECK, name="ck_companies_board"),
        sa.CheckConstraint(
            _LISTING_STATUS_CHECK,
            name="ck_companies_listing_status",
        ),
        sa.CheckConstraint(
            _EXCHANGE_BOARD_CHECK,
            name="ck_companies_exchange_board_consistency",
        ),
        sa.CheckConstraint(
            "delisting_date IS NULL OR delisting_date >= listing_date",
            name="ck_companies_dates",
        ),
        sa.UniqueConstraint("identity_key", name="uq_companies_identity_key"),
        sa.UniqueConstraint(
            "exchange",
            "security_code",
            name="uq_companies_exchange_code",
        ),
    )
    op.create_index("ix_companies_security_code", "companies", ["security_code"])
    op.create_index("ix_companies_official_name", "companies", ["official_name"])
    op.create_index("ix_companies_short_name", "companies", ["short_name"])

    op.create_table(
        "company_aliases",
        sa.Column("alias_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("alias", sa.String(length=200), nullable=False),
        sa.Column("normalized_alias", sa.String(length=200), nullable=False),
        sa.Column("alias_type", sa.String(length=32), nullable=False),
        sa.Column(
            "source_provider_key",
            sa.String(length=32),
            sa.ForeignKey("source_providers.provider_key"),
            nullable=False,
        ),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("valid_from", sa.Date(), nullable=True),
        sa.Column("valid_to", sa.Date(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_ALIAS_TYPE_CHECK, name="ck_company_aliases_type"),
        sa.CheckConstraint(
            "valid_to IS NULL OR valid_to >= valid_from",
            name="ck_company_aliases_dates",
        ),
        sa.UniqueConstraint(
            "company_id",
            "normalized_alias",
            "alias_type",
            name="uq_company_aliases_company_alias_type",
        ),
    )
    op.create_index(
        "ix_company_aliases_normalized_alias",
        "company_aliases",
        ["normalized_alias"],
    )


def downgrade() -> None:
    op.drop_index("ix_company_aliases_normalized_alias", table_name="company_aliases")
    op.drop_table("company_aliases")
    op.drop_index("ix_companies_short_name", table_name="companies")
    op.drop_index("ix_companies_official_name", table_name="companies")
    op.drop_index("ix_companies_security_code", table_name="companies")
    op.drop_table("companies")
    op.drop_index("ix_source_providers_enabled", table_name="source_providers")
    op.drop_index(
        "ix_source_providers_authority_tier",
        table_name="source_providers",
    )
    op.drop_table("source_providers")
