"""evidence-bound report audit foundation

Revision ID: 0035
Revises: 0034
Create Date: 2026-08-11

阶段 5D：Evidence-bound Agent Audit（Agent 审计）。

两张表：

1. `report_audits`——一次 **Evidence-bound 语义审计** 的聚合记录。
   - `audit_id` UUID PK；
   - `report_id` FK `reports.report_id` RESTRICT、`check_result_id` FK
     `report_check_results.check_result_id` RESTRICT——上游存在期间 Audit 不静默
     消失；Audit 不可变，无 update API；
   - `audit_schema_version`（当前 = `REPORT_AUDIT_SCHEMA_VERSION`）、`auditor_name`
     （当前 = `AUDITOR_NAME`）、`auditor_version`（当前 = `AUDITOR_VERSION`）、
     `auditor_model_id`（production = `deepseek-v4-flash`）；
   - `audit_input_fingerprint` CHAR(64) **UNIQUE**——audit schema + report /
     check 指纹 + auditor 身份 + normalized audit pack 身份（section/paragraph
     结构 + Claim/Evidence 指纹 + ClaimEvidence relation + synthesis
     conflict/gap 身份）的 SHA-256；调用 LLM 前 replay 命中 → 0 model calls；
     同 input → replay 同一行（**UNIQUE**，是并发唯一性来源）；
   - `audit_status` VARCHAR(16)（pass/fail）、`recommended_route`
     VARCHAR(24)（pass/rewrite/research/human_review）——由程序根据 resolved
     issues 确定性派生（spec O），**模型不决定 routing**；
   - `issue_count` INTEGER（>=0）、`audit_fingerprint` CHAR(64)（audit_input
     指纹 + normalized issues + status + route 的 SHA-256，**NOT UNIQUE**——
     同 input 必须 replay 同一行，由 input 指纹唯一性保证）；
   - `created_at` now()。

2. `review_issues`——一次审计的具体 issue 明细（0..50 条），resolved UUID 列表
   存在 JSONB（**不建 link table**，spec G）。
   - `review_issue_id` UUID PK；
   - `audit_id` FK `report_audits.audit_id` **ON DELETE CASCADE**——issue 只属于
     一次 audit，删除审计（仅 downgrade 空表路径）时随审计清理；
   - `ordinal` INTEGER >=1，`(audit_id, ordinal)` UNIQUE——normalize 后的
     deterministic 序号；
   - `issue_type` VARCHAR(40)、`severity` VARCHAR(16)
     （normal/high/critical，CHECK）、`section_id` VARCHAR、`paragraph_index`
     INTEGER NULL（`IS NULL OR >=0` CHECK）；
   - `message` TEXT（只描述审核问题，不写新公司事实）、`related_claim_ids` /
     `related_evidence_card_ids` JSONB（resolved UUID 列表）。

**downgrade guard**：`report_audits` 或 `review_issues` 任一存在任何行 → 拒绝
回滚。Audit 是正式 immutable research artifact（即使可确定性重放，也不在
downgrade 时静默删除历史）；alembic_version 保持 0035，数据保留。两表全部为空
时才允许回到 0034。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035"
down_revision: str | None = "0034"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_AUDITS = "report_audits"
_TABLE_ISSUES = "review_issues"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE_AUDITS,
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.report_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "check_result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_check_results.check_result_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("audit_schema_version", sa.Integer(), nullable=False),
        sa.Column("auditor_name", sa.String(), nullable=False),
        sa.Column("auditor_version", sa.Integer(), nullable=False),
        sa.Column("auditor_model_id", sa.String(), nullable=False),
        sa.Column("audit_input_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column("audit_status", sa.String(16), nullable=False),
        sa.Column("recommended_route", sa.String(24), nullable=False),
        sa.Column("issue_count", sa.Integer(), nullable=False),
        sa.Column("audit_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("audit_id", name="pk_report_audits"),
        sa.CheckConstraint(
            "audit_schema_version >= 1",
            name="ck_report_audits_audit_schema_version",
        ),
        sa.CheckConstraint(
            "auditor_version >= 1",
            name="ck_report_audits_auditor_version",
        ),
        sa.CheckConstraint(
            "issue_count >= 0",
            name="ck_report_audits_issue_count",
        ),
        sa.CheckConstraint(
            "audit_status IN ('pass','fail')",
            name="ck_report_audits_audit_status",
        ),
        sa.CheckConstraint(
            "recommended_route IN ('pass','rewrite','research','human_review')",
            name="ck_report_audits_recommended_route",
        ),
        sa.CheckConstraint(
            f"audit_input_fingerprint {_SHA256_CHECK}",
            name="ck_report_audits_audit_input_fingerprint",
        ),
        sa.CheckConstraint(
            f"audit_fingerprint {_SHA256_CHECK}",
            name="ck_report_audits_audit_fingerprint",
        ),
        sa.UniqueConstraint(
            "audit_input_fingerprint",
            name="uq_report_audits_audit_input_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE_AUDITS}_report_id",
        _TABLE_AUDITS,
        ["report_id"],
    )
    op.create_index(
        f"ix_{_TABLE_AUDITS}_check_result_id",
        _TABLE_AUDITS,
        ["check_result_id"],
    )
    op.create_index(
        f"ix_{_TABLE_AUDITS}_audit_fingerprint",
        _TABLE_AUDITS,
        ["audit_fingerprint"],
    )

    op.create_table(
        _TABLE_ISSUES,
        sa.Column(
            "review_issue_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_audits.audit_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("issue_type", sa.String(40), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("paragraph_index", sa.Integer(), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("related_claim_ids", postgresql.JSONB(), nullable=False),
        sa.Column("related_evidence_card_ids", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("review_issue_id", name="pk_review_issues"),
        sa.CheckConstraint(
            "ordinal >= 1",
            name="ck_review_issues_ordinal",
        ),
        sa.CheckConstraint(
            "paragraph_index IS NULL OR paragraph_index >= 0",
            name="ck_review_issues_paragraph_index",
        ),
        sa.CheckConstraint(
            "severity IN ('normal','high','critical')",
            name="ck_review_issues_severity",
        ),
        sa.UniqueConstraint(
            "audit_id",
            "ordinal",
            name="uq_review_issues_audit_id_ordinal",
        ),
    )
    op.create_index(
        f"ix_{_TABLE_ISSUES}_audit_id",
        _TABLE_ISSUES,
        ["audit_id"],
    )


def downgrade() -> None:
    # 数据安全：Audit 是正式 immutable research artifact，即使可确定性重放，
    # 也不在 downgrade 时静默删除历史。任一表存在任何行 → 拒绝回滚（不删除数据
    # / 不修改行），alembic_version 保持 0035。两表全部为空时才允许回到 0034。
    if _table_has_row(_TABLE_AUDITS):
        raise RuntimeError(
            "cannot downgrade migration 0035: "
            f"rows present in {_TABLE_AUDITS}; refusing to drop registered "
            "report audits (alembic_version stays 0035)"
        )
    if _table_has_row(_TABLE_ISSUES):
        raise RuntimeError(
            "cannot downgrade migration 0035: "
            f"rows present in {_TABLE_ISSUES}; refusing to drop registered "
            "review issues (alembic_version stays 0035)"
        )
    op.drop_table(_TABLE_ISSUES)
    op.drop_table(_TABLE_AUDITS)
