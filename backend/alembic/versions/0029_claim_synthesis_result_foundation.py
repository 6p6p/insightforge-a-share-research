"""claim synthesis structured result foundation

Revision ID: 0029
Revises: 0028
Create Date: 2026-08-10

阶段 4D.1B：Structured Claim Synthesis。

单表 `claim_synthesis_results` 保存一次 **不可变的结构化综合结果**：对一个
SynthesisRun（research question + analysis cutoff + 已验证 Claim 输入集，
4D.1A 已登记）做 LLM 综合后，把 themes / claim roles / duplicates / conflicts /
evidence gaps / summary 结构化落库。**不是 Report、不是 DraftSection、不是
Audit**——只记录"综合判断输出 + provenance"，供未来 Stage 5 消费。

1. `synthesis_result_id` UUID PK；
2. `synthesis_id` FK `claim_synthesis_runs.synthesis_id` **RESTRICT**——run 存在
   期间结果不静默消失（provenance 可重放）；结果不可变，无 update API；
3. `result_schema_version` >= 1（当前 = `SYNTHESIS_RESULT_SCHEMA_VERSION`）；
4. `result_fingerprint` CHAR(64) UNIQUE——同 run + 同 analyst 版本 + 同输出 →
   同指纹 → replay 同一行；任一变化 → 新指纹 → 新结果（旧行保留）；
5. `themes` / `claim_roles` / `duplicates` / `conflicts` / `evidence_gaps`
   全部 JSONB——结构化输出本体（应用层 strict validation 保证结构、no
   cherry-picking：claim_roles 恰好覆盖每条输入 Claim）；
6. `summary` Text 非空；`analyst_name` / `analyst_version` / `analyst_model_id`
   记录综合分析师 provenance；`created_at` now()。

**不复制** Evidence / Calculation / Transmission / Comparison 的 ID；不存
RawArtifact / prompt / raw provider response。**downgrade guard（spec U）**：
`claim_synthesis_results` 存在行 → 拒绝回滚（删除表会静默丢弃已登记的综合
结果与 provenance 边界）；alembic_version 保持 0029。全部为空时才允许回到
0028。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029"
down_revision: str | None = "0028"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RESULTS = "claim_synthesis_results"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _RESULTS,
        sa.Column(
            "synthesis_result_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "synthesis_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_synthesis_runs.synthesis_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("result_schema_version", sa.Integer(), nullable=False),
        sa.Column("result_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column("themes", postgresql.JSONB(), nullable=False),
        sa.Column("claim_roles", postgresql.JSONB(), nullable=False),
        sa.Column("duplicates", postgresql.JSONB(), nullable=False),
        sa.Column("conflicts", postgresql.JSONB(), nullable=False),
        sa.Column("evidence_gaps", postgresql.JSONB(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("analyst_name", sa.Text(), nullable=False),
        sa.Column("analyst_version", sa.Integer(), nullable=False),
        sa.Column("analyst_model_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("synthesis_result_id", name="pk_claim_synthesis_results"),
        sa.CheckConstraint(
            "result_schema_version >= 1",
            name="ck_claim_synthesis_results_schema_version",
        ),
        sa.CheckConstraint(
            f"result_fingerprint {_SHA256_CHECK}",
            name="ck_claim_synthesis_results_fingerprint",
        ),
        sa.CheckConstraint(
            "analyst_version >= 1",
            name="ck_claim_synthesis_results_analyst_version",
        ),
        sa.CheckConstraint(
            "btrim(summary) <> ''",
            name="ck_claim_synthesis_results_summary_not_blank",
        ),
        sa.UniqueConstraint(
            "result_fingerprint",
            name="uq_claim_synthesis_results_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_RESULTS}_synthesis_id",
        _RESULTS,
        ["synthesis_id"],
    )
    op.create_index(
        f"ix_{_RESULTS}_created_at",
        _RESULTS,
        ["created_at"],
    )


def downgrade() -> None:
    # 数据安全（spec U）：结果表存在行 → 拒绝回滚（不删除数据 / 不修改行 /
    # 不静默丢弃已登记的结构化综合结果与 provenance），alembic_version 保持
    # 0029。全部为空时才允许回到 0028。
    if _table_has_row(_RESULTS):
        raise RuntimeError(
            "cannot downgrade migration 0029: rows present in "
            "claim_synthesis_results; refusing to drop registered claim "
            "synthesis results (alembic_version stays 0029)"
        )
    op.drop_table(_RESULTS)
