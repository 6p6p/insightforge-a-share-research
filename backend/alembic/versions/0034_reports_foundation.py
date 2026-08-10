"""deterministic report assembly foundation

Revision ID: 0034
Revises: 0033
Create Date: 2026-08-10

阶段 5C：Deterministic Report Assembly + Deterministic Check（报告装配与检查）。

两张表：

1. `reports`——一次 **不可变、确定性装配**的报告正文（0 LLM）。把一次已验证
   `VerifiedReportOutline` + 显式选中的 `draft_sections`（每 Outline section
   恰好一个 DraftSection）机械拼装，**不重新生成 summary / conclusion /
   investment recommendation**（Outline 没有的 Report 不擅自增加）。
   - `report_id` UUID PK；
   - `outline_id` FK `report_outlines.outline_id` RESTRICT、`company_id` FK
     `companies.company_id` RESTRICT——上游存在期间 Report 不静默消失；Report
     不可变，无 update API；
   - `research_question_sha256` / `analysis_as_of` 派生自 outline（供检索）；
   - `report_schema_version`（当前 = `REPORT_SCHEMA_VERSION`）、`report_payload`
     JSONB（v1 = `{"sections":[{"section_id",...,"paragraphs":[...]}]}`，只存真实
     Claim/Evidence UUID + conflict/gap indexes）、`report_fingerprint` CHAR(64)
     **UNIQUE**——同 outline + 同 selected draft sections + 同 payload → replay
     同一行；任一 DraftSection / Outline 变化 → 新指纹 → 新 Report（旧行保留）；
   - `created_at` now()。

2. `report_check_results`——一次 **确定性报告检查**（10 个 v1 checks，0 LLM）的
   结构化结果。
   - `check_result_id` UUID PK；
   - `report_id` FK `reports.report_id` RESTRICT；
   - `check_schema_version`（当前 = `REPORT_CHECK_SCHEMA_VERSION`）、`status`
     VARCHAR(16)（pass/fail）、`findings` JSONB（结构化 finding，不做长 prose）、
     `check_fingerprint` CHAR(64) **UNIQUE**——check schema + report_id +
     report_fingerprint + normalized findings 的 SHA-256；同 report + 同 schema +
     同 findings → replay 同一行；Report 变化 → 新 check_fingerprint → 新
     CheckResult（旧行保留）；
   - `created_at` now()。

**downgrade guard**：`reports` 或 `report_check_results` 任一存在任何行 → 拒绝
回滚。Report / CheckResult 是正式 immutable research artifact（即使可确定性重放，
也不在 downgrade 时静默删除历史）；alembic_version 保持 0034，数据保留。两表
全部为空时才允许回到 0033。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0034"
down_revision: str | None = "0033"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_REPORTS = "reports"
_TABLE_CHECK_RESULTS = "report_check_results"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE_REPORTS,
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "outline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_outlines.outline_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("research_question_sha256", postgresql.CHAR(64), nullable=False),
        sa.Column("analysis_as_of", sa.Date(), nullable=False),
        sa.Column("report_schema_version", sa.Integer(), nullable=False),
        sa.Column("report_payload", postgresql.JSONB(), nullable=False),
        sa.Column("report_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("report_id", name="pk_reports"),
        sa.CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_reports_research_question_sha256",
        ),
        sa.CheckConstraint(
            f"report_fingerprint {_SHA256_CHECK}",
            name="ck_reports_report_fingerprint",
        ),
        sa.CheckConstraint(
            "report_schema_version >= 1",
            name="ck_reports_report_schema_version",
        ),
        sa.UniqueConstraint(
            "report_fingerprint",
            name="uq_reports_report_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE_REPORTS}_outline_id",
        _TABLE_REPORTS,
        ["outline_id"],
    )
    op.create_index(
        f"ix_{_TABLE_REPORTS}_company_id",
        _TABLE_REPORTS,
        ["company_id"],
    )
    op.create_index(
        f"ix_{_TABLE_REPORTS}_analysis_as_of",
        _TABLE_REPORTS,
        ["analysis_as_of"],
    )

    op.create_table(
        _TABLE_CHECK_RESULTS,
        sa.Column(
            "check_result_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.report_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("check_schema_version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("findings", postgresql.JSONB(), nullable=False),
        sa.Column("check_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("check_result_id", name="pk_report_check_results"),
        sa.CheckConstraint(
            f"check_fingerprint {_SHA256_CHECK}",
            name="ck_report_check_results_check_fingerprint",
        ),
        sa.CheckConstraint(
            "check_schema_version >= 1",
            name="ck_report_check_results_check_schema_version",
        ),
        sa.CheckConstraint(
            "status IN ('pass','fail')",
            name="ck_report_check_results_status",
        ),
        sa.UniqueConstraint(
            "check_fingerprint",
            name="uq_report_check_results_check_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE_CHECK_RESULTS}_report_id",
        _TABLE_CHECK_RESULTS,
        ["report_id"],
    )


def downgrade() -> None:
    # 数据安全：Report / CheckResult 是正式 immutable research artifact，即使可
    # 确定性重放，也不在 downgrade 时静默删除历史。任一表存在任何行 → 拒绝回滚
    # （不删除数据 / 不修改行），alembic_version 保持 0034。两表全部为空时才
    # 允许回到 0033。
    if _table_has_row(_TABLE_REPORTS):
        raise RuntimeError(
            "cannot downgrade migration 0034: "
            f"rows present in {_TABLE_REPORTS}; refusing to drop registered "
            "reports (alembic_version stays 0034)"
        )
    if _table_has_row(_TABLE_CHECK_RESULTS):
        raise RuntimeError(
            "cannot downgrade migration 0034: "
            f"rows present in {_TABLE_CHECK_RESULTS}; refusing to drop registered "
            "report check results (alembic_version stays 0034)"
        )
    op.drop_table(_TABLE_CHECK_RESULTS)
    op.drop_table(_TABLE_REPORTS)
