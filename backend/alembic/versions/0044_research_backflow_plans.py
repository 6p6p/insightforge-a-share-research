"""research backflow supplemental plans schema

Revision ID: 0044
Revises: 0043
Create Date: 2026-08-12

阶段 7A.2B.3：Post-Audit Supplemental Research 的确定性补充计划
（BackflowRequest → **Supplemental Plan** → supplemental Evidence → new Stage4
→ new Synthesis → BackflowFulfillment → new Stage5）。

`research_backflow_plans` 保存一次 `research_backflow_requests` 的**补充研究计划**
（immutable，v1 由 **0 LLM** 确定性生成）：

- `backflow_plan_id` UUID PK；
- `research_backflow_request_id` FK
  `research_backflow_requests.research_request_id` RESTRICT、**UNIQUE**
  （一个 research request 至多一个补充计划；并发同 request → 最终 1 行）；
- `plan_schema_version` INTEGER（当前 =
  `RESEARCH_BACKFLOW_PLAN_SCHEMA_VERSION`）；
- `strategy_name` / `strategy_version`——本次补充研究的确定性策略身份
  （need_code → strategy 映射，v1 = 仅研究已有 Source Library）；
- `plan_payload` JSONB（结构化补充计划：`need_specs[]`——need_code /
  target_section_ids / related_claim_ids / related_evidence_card_ids /
  retrieval_queries[] / allowed_source_types[] / manual_required_reason?。
  query 由确定性模板派生（research question + related Claim statement +
  research_need_code + section context），max query 冻结；**不保存** model
  reasoning / prompt / secret）；
- `plan_fingerprint` CHAR(64) **UNIQUE**（schema + request id+fingerprint +
  strategy + normalized plan_payload 的 SHA-256，同 request → replay 同一行，
  不含 backflow_plan_id / created_at）；
- `created_at` now()。

**downgrade guard**：存在任何行 → 拒绝回滚。补充计划是正式 immutable research
artifact（记录了 request 对应的确定性研究决策 / 派生 query），不在 downgrade 时
静默删除历史；alembic_version 保持 0044。表全部为空时才允许回到 0043。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0044"
down_revision: str | None = "0043"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_PLANS = "research_backflow_plans"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _PLANS,
        sa.Column(
            "backflow_plan_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "research_backflow_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_backflow_requests.research_request_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_schema_version", sa.Integer(), nullable=False),
        sa.Column("strategy_name", sa.String(64), nullable=False),
        sa.Column("strategy_version", sa.Integer(), nullable=False),
        sa.Column("plan_payload", postgresql.JSONB(), nullable=False),
        sa.Column("plan_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("backflow_plan_id", name="pk_research_backflow_plans"),
        sa.CheckConstraint(
            "plan_schema_version >= 1",
            name="ck_research_backflow_plans_plan_schema_version",
        ),
        sa.CheckConstraint(
            "btrim(strategy_name) <> ''",
            name="ck_research_backflow_plans_strategy_name_not_blank",
        ),
        sa.CheckConstraint(
            "strategy_version >= 1",
            name="ck_research_backflow_plans_strategy_version",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(plan_payload) = 'object'",
            name="ck_research_backflow_plans_plan_payload_object",
        ),
        sa.CheckConstraint(
            f"plan_fingerprint {_SHA256_CHECK}",
            name="ck_research_backflow_plans_plan_fingerprint",
        ),
        sa.UniqueConstraint(
            "research_backflow_request_id",
            name="uq_research_backflow_plans_research_backflow_request_id",
        ),
        sa.UniqueConstraint(
            "plan_fingerprint",
            name="uq_research_backflow_plans_plan_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_PLANS}_created_at",
        _PLANS,
        ["created_at"],
    )


def downgrade() -> None:
    # 数据安全：补充计划是正式 immutable research artifact（记录了 request 对应的
    # 确定性研究决策 / 派生 query），不在 downgrade 时静默删除历史。存在任何行 →
    # 拒绝回滚（不删除数据 / 不修改行），alembic_version 保持 0044。表全部为空时才
    # 允许回到 0043。
    if _table_has_row(_PLANS):
        raise RuntimeError(
            "cannot downgrade migration 0044: rows present in "
            f"{_PLANS}; refusing to drop registered supplemental research backflow "
            "plans (alembic_version stays 0044)"
        )
    op.drop_table(_PLANS)
