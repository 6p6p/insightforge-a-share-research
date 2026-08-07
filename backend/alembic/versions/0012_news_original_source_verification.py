"""news original source verification

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-07

阶段 2D.2A：
- 演进 5 个 CHECK 约束（drop 旧 + create 新，沿用 0009 的约束演进模式）：
  - source_providers.provider_type + media（第一批 Original Publishers）；
  - raw_artifacts.media_type + text/html（HTML RawArtifact 归档，保留 pdf/json）；
  - source_records.document_type + news_article；
  - source_records.acquisition_method + public_html；
  - news_discovery_candidates.verification_status 允许 unverified + verified
    （仍禁止 fact_verified/evidence/trusted，verified 语义见 ADR-0015）。
- 新建 news_source_verifications：Candidate → SourceRecord → Provider 的
  一次性验证溯源记录；同一 candidate 唯一一条，SourceRecord 可被多个
  candidate 共享（去重键 provider_key+source_url+artifact_id）。
- downgrade 对每个演进约束先检查是否存在新值数据，存在则拒绝回滚（不静默丢数据）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PROVIDER_TYPE_OLD = (
    "('exchange','regulator','statutory_disclosure_platform','issuer',"
    "'government_data','authoritative_data','international_organization',"
    "'professional_media','general_web')"
)
_PROVIDER_TYPE_NEW = (
    "('exchange','regulator','statutory_disclosure_platform','issuer',"
    "'government_data','authoritative_data','international_organization',"
    "'professional_media','general_web','media')"
)
_DOCUMENT_TYPE_OLD = (
    "('annual_report','semiannual_report','quarterly_report',"
    "'company_announcement','issuer_ir_material','prospectus','other')"
)
_DOCUMENT_TYPE_NEW = (
    "('annual_report','semiannual_report','quarterly_report',"
    "'company_announcement','issuer_ir_material','prospectus','news_article','other')"
)


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    # 1. source_providers.provider_type：+ media
    op.drop_constraint("ck_source_providers_type", "source_providers", type_="check")
    op.create_check_constraint(
        "ck_source_providers_type",
        "source_providers",
        f"provider_type IN {_PROVIDER_TYPE_NEW}",
    )

    # 2. raw_artifacts.media_type：+ text/html（保留 pdf/json）
    op.drop_constraint("ck_raw_artifacts_media_type", "raw_artifacts", type_="check")
    op.create_check_constraint(
        "ck_raw_artifacts_media_type",
        "raw_artifacts",
        "media_type IN ('application/pdf','application/json','text/html')",
    )

    # 3. source_records.document_type：+ news_article
    op.drop_constraint("ck_source_records_document_type", "source_records", type_="check")
    op.create_check_constraint(
        "ck_source_records_document_type",
        "source_records",
        f"document_type IN {_DOCUMENT_TYPE_NEW}",
    )

    # 4. source_records.acquisition_method：+ public_html
    op.drop_constraint("ck_source_records_acquisition_method", "source_records", type_="check")
    op.create_check_constraint(
        "ck_source_records_acquisition_method",
        "source_records",
        "acquisition_method IN ('user_upload','user_provided_url','public_html')",
    )

    # 5. news_discovery_candidates.verification_status：unverified + verified
    op.drop_constraint(
        "ck_news_discovery_candidates_verification_status",
        "news_discovery_candidates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_news_discovery_candidates_verification_status",
        "news_discovery_candidates",
        "verification_status IN ('unverified','verified')",
    )

    # 6. 新建 news_source_verifications
    op.create_table(
        "news_source_verifications",
        sa.Column("verification_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "candidate_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "news_discovery_candidates.candidate_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("source_records.source_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "publisher_provider_key",
            sa.String(length=32),
            sa.ForeignKey("source_providers.provider_key", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("requested_url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=False),
        sa.Column("final_hostname", sa.String(length=255), nullable=False),
        sa.Column("http_status", sa.SmallInteger(), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("redirect_count", sa.SmallInteger(), nullable=False),
        sa.Column("title_origin", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "btrim(requested_url) <> ''",
            name="ck_news_source_verifications_requested_url_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(final_url) <> ''",
            name="ck_news_source_verifications_final_url_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(final_hostname) <> ''",
            name="ck_news_source_verifications_final_hostname_not_blank",
        ),
        sa.CheckConstraint(
            "http_status BETWEEN 200 AND 299",
            name="ck_news_source_verifications_http_status",
        ),
        sa.CheckConstraint(
            "redirect_count BETWEEN 0 AND 5",
            name="ck_news_source_verifications_redirect_count",
        ),
        sa.CheckConstraint(
            "title_origin IN ('discovery_candidate')",
            name="ck_news_source_verifications_title_origin",
        ),
        sa.UniqueConstraint("candidate_id", name="uq_news_source_verifications_candidate"),
    )
    op.create_index(
        "ix_news_source_verifications_source_id",
        "news_source_verifications",
        ["source_id"],
    )
    op.create_index(
        "ix_news_source_verifications_publisher_provider_key",
        "news_source_verifications",
        ["publisher_provider_key"],
    )
    op.create_index(
        "ix_news_source_verifications_verified_at",
        "news_source_verifications",
        [sa.text("verified_at DESC")],
    )


def downgrade() -> None:
    # 1. 删除 news_source_verifications（本迁移自建表，无下游引用）
    op.drop_index(
        "ix_news_source_verifications_verified_at",
        table_name="news_source_verifications",
    )
    op.drop_index(
        "ix_news_source_verifications_publisher_provider_key",
        table_name="news_source_verifications",
    )
    op.drop_index(
        "ix_news_source_verifications_source_id",
        table_name="news_source_verifications",
    )
    op.drop_table("news_source_verifications")

    # 2. candidate.verification_status 回退到仅 unverified
    if _table_has_row("news_discovery_candidates", "verification_status = 'verified'"):
        raise RuntimeError(
            "cannot downgrade migration 0012: news_discovery_candidates contains "
            "verified rows; refusing to drop them silently"
        )
    op.drop_constraint(
        "ck_news_discovery_candidates_verification_status",
        "news_discovery_candidates",
        type_="check",
    )
    op.create_check_constraint(
        "ck_news_discovery_candidates_verification_status",
        "news_discovery_candidates",
        "verification_status = 'unverified'",
    )

    # 3. source_records.acquisition_method 回退（拒绝 public_html 数据）
    if _table_has_row("source_records", "acquisition_method = 'public_html'"):
        raise RuntimeError(
            "cannot downgrade migration 0012: source_records contains "
            "public_html rows; refusing to drop them silently"
        )
    op.drop_constraint(
        "ck_source_records_acquisition_method",
        "source_records",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_records_acquisition_method",
        "source_records",
        "acquisition_method IN ('user_upload','user_provided_url')",
    )

    # 4. source_records.document_type 回退（拒绝 news_article 数据）
    if _table_has_row("source_records", "document_type = 'news_article'"):
        raise RuntimeError(
            "cannot downgrade migration 0012: source_records contains "
            "news_article rows; refusing to drop them silently"
        )
    op.drop_constraint(
        "ck_source_records_document_type",
        "source_records",
        type_="check",
    )
    op.create_check_constraint(
        "ck_source_records_document_type",
        "source_records",
        f"document_type IN {_DOCUMENT_TYPE_OLD}",
    )

    # 5. raw_artifacts.media_type 回退（拒绝 text/html 数据）
    if _table_has_row("raw_artifacts", "media_type = 'text/html'"):
        raise RuntimeError(
            "cannot downgrade migration 0012: raw_artifacts contains "
            "text/html rows; refusing to drop them silently"
        )
    op.drop_constraint("ck_raw_artifacts_media_type", "raw_artifacts", type_="check")
    op.create_check_constraint(
        "ck_raw_artifacts_media_type",
        "raw_artifacts",
        "media_type IN ('application/pdf','application/json')",
    )

    # 6. source_providers.provider_type 回退（拒绝 media 数据）
    if _table_has_row("source_providers", "provider_type = 'media'"):
        raise RuntimeError(
            "cannot downgrade migration 0012: source_providers contains "
            "media rows; refusing to drop them silently"
        )
    op.drop_constraint("ck_source_providers_type", "source_providers", type_="check")
    op.create_check_constraint(
        "ck_source_providers_type",
        "source_providers",
        f"provider_type IN {_PROVIDER_TYPE_OLD}",
    )
