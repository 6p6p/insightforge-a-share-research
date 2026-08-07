"""macro snapshot persistence

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-07

阶段 2C.2A：
- raw_artifacts.media_type 从 PDF-only 泛化为 PDF+JSON（PDF 行为与既有归档不变；
  application/json 只用于 Macro 原始响应归档，不包装成 SourceRecord）。
- 新建四张 Macro 业务表：macro_series → macro_dataset_snapshots →
  macro_snapshot_artifacts → macro_observations。
- downgrade 明确拒绝：若 raw_artifacts 已存在 application/json 记录则抛清晰
  异常拒绝回滚，不静默丢数据。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# ---- macro_series ----
_SERIES_SOURCE_ID_CHECK = "btrim(source_id) <> ''"
_SERIES_EXTERNAL_INDICATOR_ID_CHECK = "external_indicator_id ~ '^[A-Z0-9._-]{1,64}$'"
_SERIES_GEOGRAPHY_TYPE_CHECK = "geography_type IN ('country')"
_SERIES_GEOGRAPHY_CODE_CHECK = "geography_code ~ '^[A-Z]{3}$'"
_SERIES_FREQUENCY_CHECK = "frequency IN ('annual')"
_SERIES_PROVIDER_KEY_CHECK = "btrim(provider_key) <> ''"

# ---- macro_dataset_snapshots ----
_SNAPSHOT_FINGERPRINT_CHECK = "snapshot_fingerprint ~ '^[0-9a-f]{64}$'"
_SNAPSHOT_REQUESTED_COUNTRY_CODE_CHECK = "requested_country_code ~ '^[A-Z]{2,3}$'"
_SNAPSHOT_YEAR_RANGE_CHECK = "query_start_year >= 1960 AND query_start_year <= query_end_year"
_SNAPSHOT_YEAR_SPAN_CHECK = "query_end_year - query_start_year + 1 <= 60"
_SNAPSHOT_SOURCE_ID_CHECK = "btrim(source_id_snapshot) <> ''"
_SNAPSHOT_ISO3_CHECK = "provider_country_id ~ '^[A-Z]{3}$' AND iso3_code ~ '^[A-Z]{3}$'"
_SNAPSHOT_ISO2_CHECK = "iso2_code ~ '^[A-Z]{2}$'"
_SNAPSHOT_INDICATOR_NAME_CHECK = "btrim(indicator_name) <> ''"
_SNAPSHOT_SOURCE_NAME_CHECK = "btrim(source_name) <> ''"
_SNAPSHOT_GEOGRAPHY_NAME_CHECK = "btrim(geography_name) <> ''"
_SNAPSHOT_PAGE_CHECK = "page >= 1"
_SNAPSHOT_PAGES_CHECK = "pages >= 1"
_SNAPSHOT_PAGE_LE_PAGES_CHECK = "page <= pages"
_SNAPSHOT_PER_PAGE_CHECK = "per_page >= 1"
_SNAPSHOT_PROVIDER_TOTAL_CHECK = "provider_total >= 0"
_SNAPSHOT_REQUEST_COUNT_CHECK = "request_count >= 1 AND request_count <= 20"
_SNAPSHOT_ACQUISITION_METHOD_CHECK = "acquisition_method = 'official_api'"
_SNAPSHOT_AUTHORITY_TIER_CHECK = "authority_tier_snapshot BETWEEN 1 AND 4"
_SNAPSHOT_TOPICS_ARRAY_CHECK = "jsonb_typeof(topics_snapshot) = 'array'"
_SNAPSHOT_CAPABILITIES_ARRAY_CHECK = "jsonb_typeof(provider_capabilities_snapshot) = 'array'"
_SNAPSHOT_STATUS_CHECK = "status = 'available'"

# ---- macro_snapshot_artifacts ----
_ARTIFACT_ROLE_CHECK = "role IN ('indicator_metadata','country_metadata','observations_page')"
_ARTIFACT_PAGE_RULE_CHECK = (
    "(role IN ('indicator_metadata','country_metadata') AND page IS NULL) OR "
    "(role = 'observations_page' AND page IS NOT NULL AND page >= 1)"
)
_ARTIFACT_RESPONSE_STATUS_CHECK = "response_status BETWEEN 200 AND 299"
_ARTIFACT_FINAL_HOSTNAME_CHECK = "btrim(final_hostname) <> ''"

# ---- macro_observations ----
_OBSERVATION_PERIOD_CHECK = "period ~ '^[0-9]{4}$'"
_OBSERVATION_NORMALIZED_DATE_CHECK = (
    "extract(year from normalized_period_start) = period::integer AND "
    "extract(month from normalized_period_start) = 1 AND "
    "extract(day from normalized_period_start) = 1"
)
_OBSERVATION_PERIOD_SEMANTICS_CHECK = "period_semantics = 'provider_year_label'"
_OBSERVATION_FREQUENCY_CHECK = "frequency = 'annual'"
_OBSERVATION_VALUE_IS_MISSING_CHECK = (
    "(value_numeric IS NULL AND is_missing = true) OR "
    "(value_numeric IS NOT NULL AND is_missing = false)"
)
_OBSERVATION_MISSING_SCALE_CHECK = (
    "(is_missing = true AND decimal_scale IS NULL) OR is_missing = false"
)
_OBSERVATION_PRESENT_SCALE_CHECK = (
    "(is_missing = false AND decimal_scale IS NOT NULL AND decimal_scale >= 0) OR is_missing = true"
)


def upgrade() -> None:
    # raw_artifacts.media_type：PDF-only → PDF+JSON（保留既有 PDF 数据与约束语义）
    op.drop_constraint("ck_raw_artifacts_media_type", "raw_artifacts", type_="check")
    op.create_check_constraint(
        "ck_raw_artifacts_media_type",
        "raw_artifacts",
        "media_type IN ('application/pdf','application/json')",
    )

    op.create_table(
        "macro_series",
        sa.Column("series_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "provider_key",
            sa.String(length=32),
            sa.ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("source_id", sa.String(length=32), nullable=False),
        sa.Column("external_indicator_id", sa.String(length=64), nullable=False),
        sa.Column("geography_type", sa.String(length=32), nullable=False),
        sa.Column("geography_code", sa.String(length=16), nullable=False),
        sa.Column("frequency", sa.String(length=32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_SERIES_SOURCE_ID_CHECK, name="ck_macro_series_source_id_not_blank"),
        sa.CheckConstraint(
            _SERIES_EXTERNAL_INDICATOR_ID_CHECK,
            name="ck_macro_series_external_indicator_id_format",
        ),
        sa.CheckConstraint(_SERIES_GEOGRAPHY_TYPE_CHECK, name="ck_macro_series_geography_type"),
        sa.CheckConstraint(_SERIES_GEOGRAPHY_CODE_CHECK, name="ck_macro_series_geography_code"),
        sa.CheckConstraint(_SERIES_FREQUENCY_CHECK, name="ck_macro_series_frequency"),
        sa.CheckConstraint(
            _SERIES_PROVIDER_KEY_CHECK,
            name="ck_macro_series_provider_key_not_blank",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "source_id",
            "external_indicator_id",
            "geography_type",
            "geography_code",
            "frequency",
            name="uq_macro_series_identity",
        ),
    )
    op.create_index("ix_macro_series_provider_key", "macro_series", ["provider_key"])
    op.create_index(
        "ix_macro_series_external_indicator_id",
        "macro_series",
        ["external_indicator_id"],
    )
    op.create_index("ix_macro_series_geography_code", "macro_series", ["geography_code"])
    op.create_index("ix_macro_series_frequency", "macro_series", ["frequency"])

    op.create_table(
        "macro_dataset_snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "series_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_series.series_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("snapshot_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("requested_country_code", sa.String(length=3), nullable=False),
        sa.Column("query_start_year", sa.SmallInteger(), nullable=False),
        sa.Column("query_end_year", sa.SmallInteger(), nullable=False),
        sa.Column("source_id_snapshot", sa.String(length=32), nullable=False),
        sa.Column("indicator_name", sa.String(length=500), nullable=False),
        sa.Column("indicator_unit", sa.String(length=100), nullable=False),
        sa.Column("source_name", sa.String(length=500), nullable=False),
        sa.Column("source_note", sa.Text(), nullable=False),
        sa.Column("source_organization", sa.Text(), nullable=False),
        sa.Column("topics_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column("provider_country_id", sa.String(length=3), nullable=False),
        sa.Column("iso2_code", sa.String(length=2), nullable=False),
        sa.Column("iso3_code", sa.String(length=3), nullable=False),
        sa.Column("geography_name", sa.String(length=300), nullable=False),
        sa.Column("region_name", sa.String(length=300), nullable=True),
        sa.Column("income_level_name", sa.String(length=300), nullable=True),
        sa.Column("page", sa.Integer(), nullable=False),
        sa.Column("pages", sa.Integer(), nullable=False),
        sa.Column("per_page", sa.Integer(), nullable=False),
        sa.Column("provider_total", sa.Integer(), nullable=False),
        sa.Column("provider_last_updated", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.SmallInteger(), nullable=False),
        sa.Column("acquisition_method", sa.String(length=64), nullable=False),
        sa.Column("authority_tier_snapshot", sa.SmallInteger(), nullable=False),
        sa.Column("critical_claim_eligible_snapshot", sa.Boolean(), nullable=False),
        sa.Column("provider_capabilities_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'available'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _SNAPSHOT_FINGERPRINT_CHECK,
            name="ck_macro_dataset_snapshots_fingerprint",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_REQUESTED_COUNTRY_CODE_CHECK,
            name="ck_macro_dataset_snapshots_requested_country_code",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_YEAR_RANGE_CHECK,
            name="ck_macro_dataset_snapshots_year_range",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_YEAR_SPAN_CHECK,
            name="ck_macro_dataset_snapshots_year_span",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_SOURCE_ID_CHECK,
            name="ck_macro_dataset_snapshots_source_id_not_blank",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_ISO3_CHECK,
            name="ck_macro_dataset_snapshots_iso3_format",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_ISO2_CHECK,
            name="ck_macro_dataset_snapshots_iso2_format",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_INDICATOR_NAME_CHECK,
            name="ck_macro_dataset_snapshots_indicator_name_not_blank",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_SOURCE_NAME_CHECK,
            name="ck_macro_dataset_snapshots_source_name_not_blank",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_GEOGRAPHY_NAME_CHECK,
            name="ck_macro_dataset_snapshots_geography_name_not_blank",
        ),
        sa.CheckConstraint(_SNAPSHOT_PAGE_CHECK, name="ck_macro_dataset_snapshots_page"),
        sa.CheckConstraint(_SNAPSHOT_PAGES_CHECK, name="ck_macro_dataset_snapshots_pages"),
        sa.CheckConstraint(
            _SNAPSHOT_PAGE_LE_PAGES_CHECK,
            name="ck_macro_dataset_snapshots_page_le_pages",
        ),
        sa.CheckConstraint(_SNAPSHOT_PER_PAGE_CHECK, name="ck_macro_dataset_snapshots_per_page"),
        sa.CheckConstraint(
            _SNAPSHOT_PROVIDER_TOTAL_CHECK,
            name="ck_macro_dataset_snapshots_provider_total",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_REQUEST_COUNT_CHECK,
            name="ck_macro_dataset_snapshots_request_count",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_ACQUISITION_METHOD_CHECK,
            name="ck_macro_dataset_snapshots_acquisition_method",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_AUTHORITY_TIER_CHECK,
            name="ck_macro_dataset_snapshots_authority_tier_snapshot",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_TOPICS_ARRAY_CHECK,
            name="ck_macro_dataset_snapshots_topics_array",
        ),
        sa.CheckConstraint(
            _SNAPSHOT_CAPABILITIES_ARRAY_CHECK,
            name="ck_macro_dataset_snapshots_provider_capabilities_array",
        ),
        sa.CheckConstraint(_SNAPSHOT_STATUS_CHECK, name="ck_macro_dataset_snapshots_status"),
        sa.UniqueConstraint(
            "snapshot_fingerprint",
            name="uq_macro_dataset_snapshots_fingerprint",
        ),
    )
    op.create_index(
        "ix_macro_dataset_snapshots_series_id",
        "macro_dataset_snapshots",
        ["series_id"],
    )
    op.create_index(
        "ix_macro_dataset_snapshots_fetched_at",
        "macro_dataset_snapshots",
        [sa.text("fetched_at DESC")],
    )
    op.create_index(
        "ix_macro_dataset_snapshots_requested_country_code",
        "macro_dataset_snapshots",
        ["requested_country_code"],
    )

    op.create_table(
        "macro_snapshot_artifacts",
        sa.Column(
            "snapshot_artifact_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_dataset_snapshots.snapshot_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column("final_hostname", sa.String(length=253), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_ARTIFACT_ROLE_CHECK, name="ck_macro_snapshot_artifacts_role"),
        sa.CheckConstraint(
            _ARTIFACT_PAGE_RULE_CHECK,
            name="ck_macro_snapshot_artifacts_role_page",
        ),
        sa.CheckConstraint(
            _ARTIFACT_RESPONSE_STATUS_CHECK,
            name="ck_macro_snapshot_artifacts_response_status",
        ),
        sa.CheckConstraint(
            _ARTIFACT_FINAL_HOSTNAME_CHECK,
            name="ck_macro_snapshot_artifacts_final_hostname",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "role",
            "page",
            name="uq_macro_snapshot_artifacts_snapshot_role_page",
            postgresql_nulls_not_distinct=True,
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "artifact_id",
            "role",
            "page",
            name="uq_macro_snapshot_artifacts_snapshot_artifact_role_page",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.create_index(
        "ix_macro_snapshot_artifacts_snapshot_id",
        "macro_snapshot_artifacts",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_macro_snapshot_artifacts_artifact_id",
        "macro_snapshot_artifacts",
        ["artifact_id"],
    )
    op.create_index("ix_macro_snapshot_artifacts_role", "macro_snapshot_artifacts", ["role"])

    op.create_table(
        "macro_observations",
        sa.Column("observation_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_dataset_snapshots.snapshot_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("period", sa.String(length=4), nullable=False),
        sa.Column("normalized_period_start", sa.Date(), nullable=False),
        sa.Column("period_semantics", sa.String(length=64), nullable=False),
        sa.Column("frequency", sa.String(length=32), nullable=False),
        sa.Column("value_numeric", sa.Numeric(), nullable=True),
        sa.Column("is_missing", sa.Boolean(), nullable=False),
        sa.Column("observation_status", sa.String(length=100), nullable=True),
        sa.Column("decimal_scale", sa.SmallInteger(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_OBSERVATION_PERIOD_CHECK, name="ck_macro_observations_period"),
        sa.CheckConstraint(
            _OBSERVATION_NORMALIZED_DATE_CHECK,
            name="ck_macro_observations_normalized_period_start",
        ),
        sa.CheckConstraint(
            _OBSERVATION_PERIOD_SEMANTICS_CHECK,
            name="ck_macro_observations_period_semantics",
        ),
        sa.CheckConstraint(
            _OBSERVATION_FREQUENCY_CHECK,
            name="ck_macro_observations_frequency",
        ),
        sa.CheckConstraint(
            _OBSERVATION_VALUE_IS_MISSING_CHECK,
            name="ck_macro_observations_value_is_missing",
        ),
        sa.CheckConstraint(
            _OBSERVATION_MISSING_SCALE_CHECK,
            name="ck_macro_observations_missing_scale",
        ),
        sa.CheckConstraint(
            _OBSERVATION_PRESENT_SCALE_CHECK,
            name="ck_macro_observations_present_scale",
        ),
        sa.UniqueConstraint(
            "snapshot_id",
            "period",
            name="uq_macro_observations_snapshot_period",
        ),
    )
    op.create_index("ix_macro_observations_snapshot_id", "macro_observations", ["snapshot_id"])
    op.create_index(
        "ix_macro_observations_normalized_period_start",
        "macro_observations",
        ["normalized_period_start"],
    )
    op.create_index("ix_macro_observations_period", "macro_observations", ["period"])


def downgrade() -> None:
    # 明确拒绝：raw_artifacts 已存在 application/json 归档时不允许回滚到
    # PDF-only 约束，避免静默丢失数据；给出清晰异常。
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT 1 FROM raw_artifacts WHERE media_type = 'application/json' LIMIT 1")
    )
    if rows.first() is not None:
        raise RuntimeError(
            "cannot downgrade migration 0009: raw_artifacts contains "
            "application/json artifacts; refusing to drop them silently"
        )

    op.drop_index("ix_macro_observations_period", table_name="macro_observations")
    op.drop_index(
        "ix_macro_observations_normalized_period_start",
        table_name="macro_observations",
    )
    op.drop_index("ix_macro_observations_snapshot_id", table_name="macro_observations")
    op.drop_table("macro_observations")

    op.drop_index("ix_macro_snapshot_artifacts_role", table_name="macro_snapshot_artifacts")
    op.drop_index(
        "ix_macro_snapshot_artifacts_artifact_id",
        table_name="macro_snapshot_artifacts",
    )
    op.drop_index(
        "ix_macro_snapshot_artifacts_snapshot_id",
        table_name="macro_snapshot_artifacts",
    )
    op.drop_table("macro_snapshot_artifacts")

    op.drop_index(
        "ix_macro_dataset_snapshots_requested_country_code",
        table_name="macro_dataset_snapshots",
    )
    op.drop_index(
        "ix_macro_dataset_snapshots_fetched_at",
        table_name="macro_dataset_snapshots",
    )
    op.drop_index(
        "ix_macro_dataset_snapshots_series_id",
        table_name="macro_dataset_snapshots",
    )
    op.drop_table("macro_dataset_snapshots")

    op.drop_index("ix_macro_series_frequency", table_name="macro_series")
    op.drop_index("ix_macro_series_geography_code", table_name="macro_series")
    op.drop_index(
        "ix_macro_series_external_indicator_id",
        table_name="macro_series",
    )
    op.drop_index("ix_macro_series_provider_key", table_name="macro_series")
    op.drop_table("macro_series")

    op.drop_constraint("ck_raw_artifacts_media_type", "raw_artifacts", type_="check")
    op.create_check_constraint(
        "ck_raw_artifacts_media_type",
        "raw_artifacts",
        "media_type = 'application/pdf'",
    )
