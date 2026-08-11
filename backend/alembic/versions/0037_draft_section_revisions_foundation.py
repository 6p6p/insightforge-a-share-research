"""evidence-bound draft section revision foundation

Revision ID: 0037
Revises: 0036
Create Date: 2026-08-11

阶段 5E.2A：Rewrite + Human Review 控制环的修订记录（Evidence-bound Section
Rewriter）。

单表 `draft_section_revisions`——一次 **已裁决 rewrite 的正文修订** 的 immutable
link：source 草稿 + 确定性 trigger artifact → 新正文草稿。

- `revision_id` UUID PK；
- `source_draft_section_id` FK `draft_sections.draft_section_id` RESTRICT、
  `revised_draft_section_id` FK `draft_sections.draft_section_id` RESTRICT——
  上游草稿存在期间 revision 不静默消失；`revised_draft_section_id` **UNIQUE**
  （一个修订结果只挂一条 revision link；并发同 revision → 最终 1 revised draft +
  1 revision link）；
- `revision_round` INTEGER >= 1（Stage5 loop 内轮次）、`trigger_type`
  VARCHAR(24)（deterministic_check / audit_rewrite / human_rewrite）；
- trigger 三选一（**exactly one 非空**）：`check_result_id` FK
  `report_check_results.check_result_id`、`review_action_id` FK
  `report_review_actions.review_action_id`、`human_decision_id` FK
  `human_review_decisions.human_decision_id`，其余为 NULL；
- `revision_schema_version` INTEGER（当前 = `DRAFT_SECTION_REVISION_SCHEMA_VERSION`）、
  `revision_fingerprint` CHAR(64) **UNIQUE**（派生输入 SHA-256，同 revision 输入
  → replay 同一行，不含 revision_id / created_at）；
- `source_draft_section_id <> revised_draft_section_id` CHECK；
- `created_at` now()。

**downgrade guard**：存在任何行 → 拒绝回滚。Revision 是正式 immutable research
artifact（记录了一次裁决后的正文修订，链上已存在该修订正文），不在 downgrade 时
静默删除历史；alembic_version 保持 0037，数据保留。表全部为空时才允许回到 0036。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0037"
down_revision: str | None = "0036"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "draft_section_revisions"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"

# 三个 trigger FK 中恰好一个非空（spec E）。
_EXACTLY_ONE_TRIGGER = (
    "((review_action_id IS NOT NULL)::int + "
    "(check_result_id IS NOT NULL)::int + "
    "(human_decision_id IS NOT NULL)::int) = 1"
)


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "revision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "source_draft_section_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("draft_sections.draft_section_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "revised_draft_section_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("draft_sections.draft_section_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("revision_round", sa.Integer(), nullable=False),
        sa.Column("trigger_type", sa.String(24), nullable=False),
        sa.Column(
            "review_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_review_actions.review_action_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "check_result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_check_results.check_result_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "human_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("human_review_decisions.human_decision_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("revision_schema_version", sa.Integer(), nullable=False),
        sa.Column("revision_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("revision_id", name="pk_draft_section_revisions"),
        sa.CheckConstraint(
            "revision_round >= 1",
            name="ck_draft_section_revisions_revision_round",
        ),
        sa.CheckConstraint(
            "trigger_type IN ('deterministic_check','audit_rewrite','human_rewrite')",
            name="ck_draft_section_revisions_trigger_type",
        ),
        sa.CheckConstraint(
            "revision_schema_version >= 1",
            name="ck_draft_section_revisions_revision_schema_version",
        ),
        sa.CheckConstraint(
            _EXACTLY_ONE_TRIGGER,
            name="ck_draft_section_revisions_exactly_one_trigger",
        ),
        sa.CheckConstraint(
            "source_draft_section_id <> revised_draft_section_id",
            name="ck_draft_section_revisions_source_ne_revised",
        ),
        sa.CheckConstraint(
            f"revision_fingerprint {_SHA256_CHECK}",
            name="ck_draft_section_revisions_revision_fingerprint",
        ),
        sa.UniqueConstraint(
            "revised_draft_section_id",
            name="uq_draft_section_revisions_revised_draft_section_id",
        ),
        sa.UniqueConstraint(
            "revision_fingerprint",
            name="uq_draft_section_revisions_revision_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE}_source_draft_section_id",
        _TABLE,
        ["source_draft_section_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_review_action_id",
        _TABLE,
        ["review_action_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_check_result_id",
        _TABLE,
        ["check_result_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_human_decision_id",
        _TABLE,
        ["human_decision_id"],
    )


def downgrade() -> None:
    # 数据安全：Revision 是正式 immutable research artifact（记录了裁决后的正文
    # 修订，链上已存在修订正文），不在 downgrade 时静默删除历史。存在任何行 →
    # 拒绝回滚（不删除数据 / 不修改行），alembic_version 保持 0037。表全部为空
    # 时才允许回到 0036。
    if _table_has_row(_TABLE):
        raise RuntimeError(
            "cannot downgrade migration 0037: "
            f"rows present in {_TABLE}; refusing to drop registered "
            "draft section revisions (alembic_version stays 0037)"
        )
    op.drop_table(_TABLE)
