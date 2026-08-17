"""product supply paths schema (V1.1 final closure)

Revision ID: 0049
Revises: 0048
Create Date: 2026-08-15

最终产品收口的 schema 变更：

1. `source_records.source_url` 放宽为 nullable：本地 PDF 上传允许**无官方 URL**
   （`user_upload` 来源不伪造 URL）；URL 相关 CHECK 约束改为 NULL 容忍。
2. `source_records.acquisition_method` 增加 `'automatic_discovery'`：受控自动
   获取（公告 discovery）的来源方式。
3. `evidence_cards` 增加 `origin_type='user_supplied'`：用户从官方报告转录的
   财务数值证据（quote = 用户粘贴的原文引文，source = user_supplied 来源
   记录；**可信级别 Tier-4、critical_claim_eligible=False，绝不伪装成官方
   自动提取**）。
4. 新表 `issuer_domains`：上市公司官网域名 registry（company_id 绑定、
   真实域名、验证 URL、provenance）——`issuer_official` 受控来源的域名
   校验依据（不降低现有 allowlist / SSRF 策略）。

**downgrade guard**：存在 user_supplied 卡 / automatic_discovery 来源 /
NULL source_url / issuer_domains 行时拒绝回滚（正式 provenance 不静默删除）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0049"
down_revision: str | None = "0048"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_TYPE_CHECK = "origin_type IN ('document_chunk','macro_observation','user_supplied')"
_ORIGIN_CONSISTENCY_CHECK = """
(
  (origin_type = 'document_chunk' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     chunk_set_id IS NOT NULL AND chunk_id IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = 'macro_observation' AND
     macro_observation_id IS NOT NULL AND macro_snapshot_id IS NOT NULL AND
     macro_series_id IS NOT NULL AND
     source_id IS NULL AND parsed_source_id IS NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     quote_text IS NULL AND quote_sha256 IS NULL)
  OR
  (origin_type = 'user_supplied' AND
     source_id IS NOT NULL AND quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     parsed_source_id IS NULL AND chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
)
"""

_ACQUISITION_METHOD_CHECK = (
    "acquisition_method IN ('user_upload','user_provided_url','public_html',"
    "'automatic_discovery','user_supplied')"
)

# downgrade 重建用的 0017 原始 CHECK 定义（document + macro 双 origin）。
_DOWNGRADE_ORIGIN_TYPE_CHECK = "origin_type IN ('document_chunk','macro_observation')"
_DOWNGRADE_ORIGIN_CONSISTENCY_CHECK = """
(
  (origin_type = 'document_chunk' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     chunk_set_id IS NOT NULL AND chunk_id IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = 'macro_observation' AND
     macro_observation_id IS NOT NULL AND macro_snapshot_id IS NOT NULL AND
     macro_series_id IS NOT NULL AND
     source_id IS NULL AND parsed_source_id IS NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     quote_text IS NULL AND quote_sha256 IS NULL)
)
"""


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def _constraint_exists(table: str, name: str) -> bool:
    """pg_constraint 存在性检查（防御部分应用 / 中断后重放的状态）。"""
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT 1 FROM pg_constraint WHERE conname = :name "
            "AND conrelid = to_regclass(:table_name) LIMIT 1"
        ),
        {"name": name, "table_name": table},
    )
    return rows.first() is not None


def _drop_constraint_if_exists(table: str, name: str, type_: str) -> None:
    if _constraint_exists(table, name):
        op.drop_constraint(name, table, type_=type_)


def upgrade() -> None:
    # 1. source_records：source_url nullable + URL CHECK 放宽。
    op.alter_column("source_records", "source_url", nullable=True)
    _drop_constraint_if_exists("source_records", "ck_source_records_url_https", "check")
    _drop_constraint_if_exists("source_records", "ck_source_records_url_no_userinfo", "check")
    _drop_constraint_if_exists("source_records", "ck_source_records_url_no_fragment", "check")
    op.create_check_constraint(
        "ck_source_records_url_https",
        "source_records",
        "source_url IS NULL OR source_url ~ '^https://'",
    )
    op.create_check_constraint(
        "ck_source_records_url_no_userinfo",
        "source_records",
        "source_url IS NULL OR source_url !~ '://[^/]*@'",
    )
    op.create_check_constraint(
        "ck_source_records_url_no_fragment",
        "source_records",
        "source_url IS NULL OR position('#' in source_url) = 0",
    )

    # 2. acquisition_method 扩展。
    _drop_constraint_if_exists("source_records", "ck_source_records_acquisition_method", "check")
    op.create_check_constraint(
        "ck_source_records_acquisition_method",
        "source_records",
        _ACQUISITION_METHOD_CHECK,
    )

    # 3. evidence_cards：origin_type + 一致性 CHECK 扩展（user_supplied）。
    _drop_constraint_if_exists("evidence_cards", "ck_evidence_cards_origin_type", "check")
    # 0017 已创建 origin_consistency / locator_refs_nonempty（document+macro 分支），
    # 必须先 drop 再以扩展定义重建。
    _drop_constraint_if_exists("evidence_cards", "ck_evidence_cards_origin_consistency", "check")
    op.create_check_constraint(
        "ck_evidence_cards_origin_type",
        "evidence_cards",
        _ORIGIN_TYPE_CHECK,
    )
    op.create_check_constraint(
        "ck_evidence_cards_origin_consistency",
        "evidence_cards",
        _ORIGIN_CONSISTENCY_CHECK,
    )
    # user_supplied 卡没有 chunk locator → 允许空 locator_refs。
    _drop_constraint_if_exists("evidence_cards", "ck_evidence_cards_locator_refs_nonempty", "check")
    op.create_check_constraint(
        "ck_evidence_cards_locator_refs_nonempty",
        "evidence_cards",
        "origin_type = 'user_supplied' OR jsonb_array_length(locator_refs) > 0",
    )

    # 4. issuer_domains 表 + snapshot provenance（mirror company_master 模式）。
    op.create_table(
        "issuer_domain_snapshots",
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("snapshot_version", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("company_count", sa.Integer(), nullable=False),
        sa.Column("domain_count", sa.Integer(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "company_count >= 0 AND domain_count >= 0",
            name="ck_issuer_domain_snapshots_counts",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_issuer_domain_snapshots_sha256",
        ),
        sa.UniqueConstraint(
            "snapshot_version",
            "content_sha256",
            name="uq_issuer_domain_snapshots_version_sha",
        ),
    )
    op.create_index(
        "ix_issuer_domain_snapshots_version",
        "issuer_domain_snapshots",
        ["snapshot_version"],
    )
    op.create_table(
        "issuer_domains",
        sa.Column("domain_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("domain", sa.String(length=200), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("provider_key", sa.String(length=32), nullable=False),
        sa.Column("verified_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "btrim(domain) <> ''",
            name="ck_issuer_domains_domain_not_blank",
        ),
        sa.CheckConstraint(
            "source_url ~ '^https://'",
            name="ck_issuer_domains_url_https",
        ),
        sa.UniqueConstraint(
            "company_id",
            "domain",
            name="uq_issuer_domains_company_domain",
        ),
    )
    op.create_index("ix_issuer_domains_domain", "issuer_domains", ["domain"])


def downgrade() -> None:
    # guard：正式 provenance 不静默删除。
    if _table_has_row("evidence_cards", "origin_type = 'user_supplied'"):
        raise RuntimeError("user_supplied evidence cards exist; refusing silent data loss")
    if _table_has_row(
        "source_records", "acquisition_method IN ('automatic_discovery','user_supplied')"
    ):
        raise RuntimeError(
            "automatic_discovery/user_supplied source records exist; refusing silent data loss"
        )
    if _table_has_row("source_records", "source_url IS NULL"):
        raise RuntimeError("source records with NULL source_url exist; refusing data loss")
    if _table_has_row("issuer_domains", "1 = 1"):
        raise RuntimeError("issuer domain rows exist; refusing silent data loss")
    if _table_has_row("issuer_domain_snapshots", "1 = 1"):
        raise RuntimeError(
            "issuer domain snapshot provenance rows exist; refusing silent data loss"
        )

    op.drop_index("ix_issuer_domain_snapshots_version", table_name="issuer_domain_snapshots")
    op.drop_table("issuer_domain_snapshots")
    op.drop_index("ix_issuer_domains_domain", table_name="issuer_domains")
    op.drop_table("issuer_domains")
    op.drop_constraint("ck_evidence_cards_origin_consistency", "evidence_cards", type_="check")
    op.drop_constraint("ck_evidence_cards_origin_type", "evidence_cards", type_="check")
    op.create_check_constraint(
        "ck_evidence_cards_origin_type",
        "evidence_cards",
        _DOWNGRADE_ORIGIN_TYPE_CHECK,
    )
    op.create_check_constraint(
        "ck_evidence_cards_origin_consistency",
        "evidence_cards",
        _DOWNGRADE_ORIGIN_CONSISTENCY_CHECK,
    )
    op.drop_constraint("ck_evidence_cards_locator_refs_nonempty", "evidence_cards", type_="check")
    op.create_check_constraint(
        "ck_evidence_cards_locator_refs_nonempty",
        "evidence_cards",
        "jsonb_array_length(locator_refs) > 0",
    )
    op.drop_constraint("ck_source_records_acquisition_method", "source_records", type_="check")
    op.create_check_constraint(
        "ck_source_records_acquisition_method",
        "source_records",
        "acquisition_method IN ('user_upload','user_provided_url','public_html')",
    )
    op.drop_constraint("ck_source_records_url_https", "source_records", type_="check")
    op.drop_constraint("ck_source_records_url_no_userinfo", "source_records", type_="check")
    op.drop_constraint("ck_source_records_url_no_fragment", "source_records", type_="check")
    op.create_check_constraint(
        "ck_source_records_url_https",
        "source_records",
        "source_url ~ '^https://'",
    )
    op.create_check_constraint(
        "ck_source_records_url_no_userinfo",
        "source_records",
        "source_url !~ '://[^/]*@'",
    )
    op.create_check_constraint(
        "ck_source_records_url_no_fragment",
        "source_records",
        "position('#' in source_url) = 0",
    )
    op.alter_column("source_records", "source_url", nullable=False)
