"""news discovery records

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-07

阶段 2D.1：
- 新建 news_discovery_runs：一次"搜索-发现"过程的审计痕迹（Engine、搜索表达式、
  时间窗、max_results、请求次数、原始响应归档引用与冗余响应元数据）。
- 新建 news_discovery_candidates：同一 run 下的候选结果列表（rank 唯一、
  normalized_url 唯一、url_sha256 内容寻址）。
- query_fingerprint 唯一标识"engine + company_id + query_text + 时间范围 +
  max_results + raw response sha256"的确定性版本，重复响应 replay 同一 Run。
- 两张表都只存"发现过程"与"候选线索"，不表示任何原始新闻已验证；
  engine 不引用 SourceProvider（GDELT 不进 Source Registry）。
- downgrade 直接删除两张表（本迁移是新增表，无既有业务数据需要保护）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

# ---- news_discovery_runs ----
_RUN_ENGINE_CHECK = "engine = 'gdelt_doc'"
_RUN_QUERY_TEXT_CHECK = "btrim(query_text) <> ''"
_RUN_WINDOW_ORDER_CHECK = "query_start_at <= query_end_at"
_RUN_MAX_RESULTS_CHECK = "max_results BETWEEN 1 AND 100"
_RUN_RAW_SHA256_CHECK = f"raw_content_sha256 {_SHA256_CHECK}"
_RUN_FINGERPRINT_CHECK = f"query_fingerprint {_SHA256_CHECK}"
_RUN_RESULT_COUNT_CHECK = "result_count >= 0"
_RUN_REQUEST_COUNT_CHECK = "request_count >= 1"
_RUN_RESPONSE_STATUS_CHECK = "response_status BETWEEN 200 AND 299"
_RUN_FINAL_HOSTNAME_CHECK = "btrim(final_hostname) <> ''"
_RUN_STATUS_CHECK = "status = 'available'"

# ---- news_discovery_candidates ----
_CANDIDATE_RANK_CHECK = "rank >= 1"
_CANDIDATE_TITLE_CHECK = "btrim(title) <> ''"
_CANDIDATE_NORMALIZED_URL_CHECK = "btrim(normalized_url) <> ''"
_CANDIDATE_URL_SHA256_CHECK = f"url_sha256 {_SHA256_CHECK}"
_CANDIDATE_DOMAIN_CHECK = "btrim(domain) <> ''"
_CANDIDATE_VERIFICATION_STATUS_CHECK = "verification_status = 'unverified'"


def upgrade() -> None:
    op.create_table(
        "news_discovery_runs",
        sa.Column("discovery_run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("engine", sa.String(length=32), nullable=False),
        sa.Column("query_text", sa.String(length=300), nullable=False),
        sa.Column("query_start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query_end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_results", sa.SmallInteger(), nullable=False),
        sa.Column(
            "raw_artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("raw_artifacts.artifact_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("raw_content_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("result_count", sa.Integer(), nullable=False),
        sa.Column("request_count", sa.SmallInteger(), nullable=False),
        sa.Column("response_status", sa.SmallInteger(), nullable=False),
        sa.Column("final_hostname", sa.String(length=253), nullable=False),
        sa.Column("content_type", sa.String(length=100), nullable=False),
        sa.Column("query_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            server_default=sa.text("'available'"),
            nullable=False,
        ),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_RUN_ENGINE_CHECK, name="ck_news_discovery_runs_engine"),
        sa.CheckConstraint(
            _RUN_QUERY_TEXT_CHECK,
            name="ck_news_discovery_runs_query_text_not_blank",
        ),
        sa.CheckConstraint(_RUN_WINDOW_ORDER_CHECK, name="ck_news_discovery_runs_window_order"),
        sa.CheckConstraint(_RUN_MAX_RESULTS_CHECK, name="ck_news_discovery_runs_max_results"),
        sa.CheckConstraint(
            _RUN_RAW_SHA256_CHECK,
            name="ck_news_discovery_runs_raw_content_sha256",
        ),
        sa.CheckConstraint(
            _RUN_FINGERPRINT_CHECK,
            name="ck_news_discovery_runs_query_fingerprint",
        ),
        sa.CheckConstraint(_RUN_RESULT_COUNT_CHECK, name="ck_news_discovery_runs_result_count"),
        sa.CheckConstraint(_RUN_REQUEST_COUNT_CHECK, name="ck_news_discovery_runs_request_count"),
        sa.CheckConstraint(
            _RUN_RESPONSE_STATUS_CHECK,
            name="ck_news_discovery_runs_response_status",
        ),
        sa.CheckConstraint(
            _RUN_FINAL_HOSTNAME_CHECK,
            name="ck_news_discovery_runs_final_hostname_not_blank",
        ),
        sa.CheckConstraint(_RUN_STATUS_CHECK, name="ck_news_discovery_runs_status"),
        sa.UniqueConstraint(
            "query_fingerprint",
            name="uq_news_discovery_runs_query_fingerprint",
        ),
    )
    op.create_index(
        "ix_news_discovery_runs_company_id",
        "news_discovery_runs",
        ["company_id"],
    )
    op.create_index(
        "ix_news_discovery_runs_fetched_at",
        "news_discovery_runs",
        [sa.text("fetched_at DESC")],
    )
    op.create_index(
        "ix_news_discovery_runs_raw_artifact_id",
        "news_discovery_runs",
        ["raw_artifact_id"],
    )

    op.create_table(
        "news_discovery_candidates",
        sa.Column("candidate_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "discovery_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("news_discovery_runs.discovery_run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.SmallInteger(), nullable=False),
        sa.Column("title", sa.String(length=1000), nullable=False),
        sa.Column("discovered_url", sa.Text(), nullable=False),
        sa.Column("normalized_url", sa.Text(), nullable=False),
        sa.Column("url_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("domain", sa.String(length=253), nullable=False),
        sa.Column("seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_language", sa.String(length=100), nullable=True),
        sa.Column("source_country", sa.String(length=100), nullable=True),
        sa.Column(
            "verification_status",
            sa.String(length=32),
            server_default=sa.text("'unverified'"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_CANDIDATE_RANK_CHECK, name="ck_news_discovery_candidates_rank"),
        sa.CheckConstraint(
            _CANDIDATE_TITLE_CHECK,
            name="ck_news_discovery_candidates_title_not_blank",
        ),
        sa.CheckConstraint(
            _CANDIDATE_NORMALIZED_URL_CHECK,
            name="ck_news_discovery_candidates_normalized_url_not_blank",
        ),
        sa.CheckConstraint(
            _CANDIDATE_URL_SHA256_CHECK,
            name="ck_news_discovery_candidates_url_sha256",
        ),
        sa.CheckConstraint(
            _CANDIDATE_DOMAIN_CHECK,
            name="ck_news_discovery_candidates_domain_not_blank",
        ),
        sa.CheckConstraint(
            _CANDIDATE_VERIFICATION_STATUS_CHECK,
            name="ck_news_discovery_candidates_verification_status",
        ),
        sa.UniqueConstraint(
            "discovery_run_id",
            "rank",
            name="uq_news_discovery_candidates_run_rank",
        ),
        sa.UniqueConstraint(
            "discovery_run_id",
            "normalized_url",
            name="uq_news_discovery_candidates_run_normalized_url",
        ),
    )
    op.create_index(
        "ix_news_discovery_candidates_run_id",
        "news_discovery_candidates",
        ["discovery_run_id"],
    )
    op.create_index(
        "ix_news_discovery_candidates_domain",
        "news_discovery_candidates",
        ["domain"],
    )
    op.create_index(
        "ix_news_discovery_candidates_seen_at",
        "news_discovery_candidates",
        [sa.text("seen_at DESC")],
    )
    op.create_index(
        "ix_news_discovery_candidates_url_sha256",
        "news_discovery_candidates",
        ["url_sha256"],
    )


def downgrade() -> None:
    op.drop_index("ix_news_discovery_candidates_url_sha256", table_name="news_discovery_candidates")
    op.drop_index("ix_news_discovery_candidates_seen_at", table_name="news_discovery_candidates")
    op.drop_index("ix_news_discovery_candidates_domain", table_name="news_discovery_candidates")
    op.drop_index("ix_news_discovery_candidates_run_id", table_name="news_discovery_candidates")
    op.drop_table("news_discovery_candidates")
    op.drop_index("ix_news_discovery_runs_raw_artifact_id", table_name="news_discovery_runs")
    op.drop_index("ix_news_discovery_runs_fetched_at", table_name="news_discovery_runs")
    op.drop_index("ix_news_discovery_runs_company_id", table_name="news_discovery_runs")
    op.drop_table("news_discovery_runs")
