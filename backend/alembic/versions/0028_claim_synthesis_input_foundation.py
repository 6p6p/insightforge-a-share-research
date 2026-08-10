"""claim synthesis input + provenance foundation

Revision ID: 0028
Revises: 0027
Create Date: 2026-08-10

阶段 4D.1A：Claim Synthesis Input & Provenance Foundation。

两张表登记一次 **Claim Synthesis 综合输入**：一个 research question + 一个
analysis cutoff 下，把调用方显式选出的 2..50 条 Claim（跨 analysis_domain：
financial / macro / valuation / business / event / risk）绑定为一个不可变
集合，供未来 LangGraph 合成节点消费。**不是 Report / DraftSection**。

1. `claim_synthesis_runs`——run 头（synthesis_id PK；company_id FK companies
   RESTRICT；research_question + research_question_sha256；analysis_as_of；
   synthesis_schema_version >= 1；synthesis_fingerprint UNIQUE——同一完全相同
   input → replay 同一 run，input 顺序不影响指纹；created_at）。CHECK
   sha256 / fingerprint 64 位小写 hex、question trim 非空。INDEX company_id /
   analysis_as_of。
2. `claim_synthesis_input_links`——run ↔ Claim 输入集边界（PK(synthesis_id,
   claim_id)；synthesis_id FK runs **CASCADE**；claim_id FK claims
   **RESTRICT**——Claim 存在期间 link 不静默消失，保证 provenance 可重放；
   INDEX claim_id 反查引用）。

**不复制** Evidence / Calculation / Transmission / Comparison 的 ID 到 synthesis
表：Claim → 各 domain 子表 → Evidence → Source 的 provenance 已在既有 schema 中
（本表只引用 claims.claim_id，证明输入集边界）。

**downgrade guard（spec U）**：`claim_synthesis_runs` 或
`claim_synthesis_input_links` 任一存在行 → 拒绝回滚（删除表会静默丢弃已登记的
synthesis 输入集与 provenance 边界）；alembic_version 保持 0028。全部为空时
才允许回到 0027。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0028"
down_revision: str | None = "0027"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_RUNS = "claim_synthesis_runs"
_LINKS = "claim_synthesis_input_links"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

_ALL_TABLES = (_RUNS, _LINKS)


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _RUNS,
        sa.Column(
            "synthesis_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("research_question", sa.Text(), nullable=False),
        sa.Column("research_question_sha256", postgresql.CHAR(64), nullable=False),
        sa.Column("analysis_as_of", sa.Date(), nullable=False),
        sa.Column("synthesis_schema_version", sa.Integer(), nullable=False),
        sa.Column("synthesis_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("synthesis_id", name="pk_claim_synthesis_runs"),
        sa.CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_claim_synthesis_runs_research_question_sha256",
        ),
        sa.CheckConstraint(
            f"synthesis_fingerprint {_SHA256_CHECK}",
            name="ck_claim_synthesis_runs_synthesis_fingerprint",
        ),
        sa.CheckConstraint(
            "synthesis_schema_version >= 1",
            name="ck_claim_synthesis_runs_schema_version",
        ),
        sa.CheckConstraint(
            "btrim(research_question) <> ''",
            name="ck_claim_synthesis_runs_research_question_not_blank",
        ),
        sa.UniqueConstraint(
            "synthesis_fingerprint",
            name="uq_claim_synthesis_runs_synthesis_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_RUNS}_company_id",
        _RUNS,
        ["company_id"],
    )
    op.create_index(
        f"ix_{_RUNS}_analysis_as_of",
        _RUNS,
        ["analysis_as_of"],
    )

    op.create_table(
        _LINKS,
        sa.Column(
            "synthesis_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_synthesis_runs.synthesis_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.claim_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint(
            "synthesis_id",
            "claim_id",
            name="pk_claim_synthesis_input_links",
        ),
    )
    op.create_index(
        f"ix_{_LINKS}_claim_id",
        _LINKS,
        ["claim_id"],
    )


def downgrade() -> None:
    # 数据安全（spec U）：run 或 link 任一存在行 → 拒绝回滚（不删除数据 /
    # 不修改行 / 不静默丢弃已登记的 synthesis 输入集与 provenance 边界），
    # alembic_version 保持 0028。全部为空时才允许回到 0027。
    for table in _ALL_TABLES:
        if _table_has_row(table):
            raise RuntimeError(
                "cannot downgrade migration 0028: "
                f"rows present in {table}; refusing to drop registered claim "
                "synthesis input runs / input links "
                "(alembic_version stays 0028)"
            )
    op.drop_table(_LINKS)
    op.drop_table(_RUNS)
