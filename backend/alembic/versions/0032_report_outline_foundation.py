"""deterministic report outline foundation

Revision ID: 0032
Revises: 0031
Create Date: 2026-08-10

阶段 5A：Deterministic Report Outline（报告提纲基础）。

单表 `report_outlines` 保存一次 **不可变、确定性派生**的报告提纲：把已验证的
SynthesisResult（claim_synthesis_results，4D.1B）机械地映射为结构化提纲
（每个 theme → theme section；conflicts / evidence gaps → risks_and_gaps
section 存 indexes），**不调用 LLM 规划提纲**（0 planner model / 0 analyst
version）。提纲不是 Report / DraftSection / Audit 正文。

1. `outline_id` UUID PK；
2. `synthesis_result_id` FK `claim_synthesis_results.synthesis_result_id`
   **RESTRICT**——result 存在期间提纲不静默消失；提纲不可变，无 update API；
3. `company_id` FK `companies.company_id` RESTRICT；`research_question_sha256`
   CHAR(64)；`analysis_as_of` DATE——派生自 synthesis run，供检索；
4. `outline_schema_version` >= 1（当前 = `REPORT_OUTLINE_SCHEMA_VERSION`）；
5. `outline_payload` JSONB——v1 = `{"sections":[...]}`（section_id /
   section_type / title / claim_ids / conflict_indexes / evidence_gap_indexes /
   section_order）；
6. `outline_fingerprint` CHAR(64) UNIQUE——同 synthesis result + 同 schema + 同
   normalized payload → 同指纹 → replay 同一行；SynthesisResult 变化 → 新指纹 →
   新提纲（旧行保留，无 update API）；
7. `created_at` now()。

**downgrade guard**：`report_outlines` 存在任何行 → 拒绝回滚。ReportOutline 是
正式 immutable research artifact（即使可确定性重放，也不在 downgrade 时静默
删除历史）；alembic_version 保持 0032，数据保留。全部为空时才允许回到 0031。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0032"
down_revision: str | None = "0031"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "report_outlines"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "outline_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "synthesis_result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_synthesis_results.synthesis_result_id", ondelete="RESTRICT"),
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
        sa.Column("outline_schema_version", sa.Integer(), nullable=False),
        sa.Column("outline_payload", postgresql.JSONB(), nullable=False),
        sa.Column("outline_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("outline_id", name="pk_report_outlines"),
        sa.CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_report_outlines_research_question_sha256",
        ),
        sa.CheckConstraint(
            f"outline_fingerprint {_SHA256_CHECK}",
            name="ck_report_outlines_fingerprint",
        ),
        sa.CheckConstraint(
            "outline_schema_version >= 1",
            name="ck_report_outlines_schema_version",
        ),
        sa.UniqueConstraint("outline_fingerprint", name="uq_report_outlines_fingerprint"),
    )
    op.create_index(
        f"ix_{_TABLE}_synthesis_result_id",
        _TABLE,
        ["synthesis_result_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_company_id",
        _TABLE,
        ["company_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_analysis_as_of",
        _TABLE,
        ["analysis_as_of"],
    )


def downgrade() -> None:
    # 数据安全：ReportOutline 是正式 immutable research artifact，即使可确定性
    # 重放，也不在 downgrade 时静默删除历史。存在任何行 → 拒绝回滚（不删除
    # 数据 / 不修改行），alembic_version 保持 0032。全部为空时才允许回到 0031。
    if _table_has_row(_TABLE):
        raise RuntimeError(
            "cannot downgrade migration 0032: "
            f"rows present in {_TABLE}; refusing to drop registered report "
            "outlines (alembic_version stays 0032)"
        )
    op.drop_table(_TABLE)
