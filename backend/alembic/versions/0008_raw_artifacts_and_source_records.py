"""raw artifacts and source records

Revision ID: 0008
Revises: 0007
Create Date: 2026-08-07

主键契约：raw_artifacts.artifact_id 与 source_records.source_id 都由 Python 层
uuid4 生成（模型 default=uuid.uuid4），不使用 DB 端 gen_random_uuid() server_default。
曾临时引入 server_default 排查 psycopg 空字节报错，根因是 chr(0) CHECK 求值
（见 _STORAGE_KEY_CHECK 注释与 ADR-0009），与 UUID 无关，故回滚该临时改动。

source_records.provider_capabilities_snapshot 保存登记时 Provider 的能力快照
（稳定排序字符串数组），不随 Provider 后续策略修改而变化。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# 注意：不使用 position(chr(0) in storage_key) = 0 检查 NUL。
# PostgreSQL 的 chr(0) 求值本身就会抛出 "null character not permitted"(54000)，
# 且 PG 文本类型天然禁止存储 NUL（含 NUL 的参数在 bind 阶段即报错），
# 因此该检查冗余且会导致所有合法 INSERT 失败。
_STORAGE_KEY_CHECK = (
    "storage_key <> '' AND "
    "storage_key !~ '^[/\\\\]' AND "
    "storage_key !~ '(^|[/\\\\])\\.\\.([/\\\\]|$)'"
)
_DOCUMENT_TYPE_CHECK = (
    "document_type IN ('annual_report','semiannual_report','quarterly_report',"
    "'company_announcement','issuer_ir_material','prospectus','other')"
)
_ACQUISITION_METHOD_CHECK = "acquisition_method IN ('user_upload','user_provided_url')"
_STATUS_CHECK = "status IN ('available')"


def upgrade() -> None:
    op.create_table(
        "raw_artifacts",
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_raw_artifacts_sha256",
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name="ck_raw_artifacts_byte_size",
        ),
        sa.CheckConstraint(
            "media_type = 'application/pdf'",
            name="ck_raw_artifacts_media_type",
        ),
        sa.CheckConstraint(_STORAGE_KEY_CHECK, name="ck_raw_artifacts_storage_key"),
        sa.UniqueConstraint(
            "content_sha256",
            name="uq_raw_artifacts_content_sha256",
        ),
        sa.UniqueConstraint(
            "storage_key",
            name="uq_raw_artifacts_storage_key",
        ),
    )

    op.create_table(
        "source_records",
        sa.Column("source_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "provider_key",
            sa.String(length=32),
            sa.ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("document_type", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=500), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reporting_period_end", sa.Date(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("acquisition_method", sa.String(length=64), nullable=False),
        sa.Column("external_document_id", sa.String(length=200), nullable=True),
        sa.Column("authority_tier_snapshot", sa.SmallInteger(), nullable=False),
        sa.Column("critical_claim_eligible_snapshot", sa.Boolean(), nullable=False),
        sa.Column("provider_capabilities_snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'available'"),
            nullable=False,
        ),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _DOCUMENT_TYPE_CHECK,
            name="ck_source_records_document_type",
        ),
        sa.CheckConstraint(
            _ACQUISITION_METHOD_CHECK,
            name="ck_source_records_acquisition_method",
        ),
        sa.CheckConstraint(_STATUS_CHECK, name="ck_source_records_status"),
        sa.CheckConstraint(
            "authority_tier_snapshot BETWEEN 1 AND 4",
            name="ck_source_records_authority_tier_snapshot",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(provider_capabilities_snapshot) = 'array'",
            name="ck_source_records_provider_capabilities_array",
        ),
        sa.CheckConstraint(
            "btrim(title) <> ''",
            name="ck_source_records_title_not_blank",
        ),
        sa.CheckConstraint(
            "source_url ~ '^https://'",
            name="ck_source_records_url_https",
        ),
        sa.CheckConstraint(
            "source_url !~ '://[^/]*@'",
            name="ck_source_records_url_no_userinfo",
        ),
        sa.CheckConstraint(
            "position('#' in source_url) = 0",
            name="ck_source_records_url_no_fragment",
        ),
        sa.UniqueConstraint(
            "provider_key",
            "source_url",
            "artifact_id",
            name="uq_source_records_provider_url_artifact",
        ),
    )
    op.create_index(
        "ix_source_records_company_id",
        "source_records",
        ["company_id"],
    )
    op.create_index(
        "ix_source_records_provider_key",
        "source_records",
        ["provider_key"],
    )
    op.create_index(
        "ix_source_records_artifact_id",
        "source_records",
        ["artifact_id"],
    )
    op.create_index(
        "ix_source_records_published_at",
        "source_records",
        ["published_at"],
    )
    op.create_index(
        "ix_source_records_document_type",
        "source_records",
        ["document_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_source_records_document_type", table_name="source_records")
    op.drop_index("ix_source_records_published_at", table_name="source_records")
    op.drop_index("ix_source_records_artifact_id", table_name="source_records")
    op.drop_index("ix_source_records_provider_key", table_name="source_records")
    op.drop_index("ix_source_records_company_id", table_name="source_records")
    op.drop_table("source_records")
    op.drop_table("raw_artifacts")
