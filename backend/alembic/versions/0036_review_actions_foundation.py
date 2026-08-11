"""report review routing + human confirmation foundation

Revision ID: 0036
Revises: 0035
Create Date: 2026-08-11

阶段 5E.1：Review Routing + Human Confirmation Foundation（确定性控制层）。

三张表：

1. `report_review_actions`——从 `VerifiedReportAudit` 确定性派生的 ReviewActionPlan。
   - `review_action_id` UUID PK；
   - `audit_id` FK `report_audits.audit_id` RESTRICT、`report_id` FK
     `reports.report_id` RESTRICT——上游存在期间 Action 不静默消失；
   - `action_schema_version`（当前 = `REVIEW_ACTION_SCHEMA_VERSION`）、
     `action_type` VARCHAR(24)（finalize/rewrite/research/human_review，由程序
     根据 audit status / recommended_route 派生，spec F）；
   - `action_payload` JSONB（source ids / target_section_ids / review_issue_ids /
     research 专用 related ids + research_need_codes，**不写长 prose**）；
   - `action_fingerprint` CHAR(64) **UNIQUE** = schema + audit_id +
     audit_fingerprint + report_id + report_fingerprint + action_type +
     normalized payload 的 SHA-256（**不含** review_action_id / created_at）；
   - `(audit_id)` **UNIQUE**——同一 immutable Audit 只能产生一个 deterministic
     Action；`created_at` now()。

2. `human_review_requests`——仅当 action_type=human_review 时创建（服务层保证）。
   - `human_request_id` UUID PK；
   - `review_action_id` FK `report_review_actions.review_action_id` RESTRICT、
     `(review_action_id)` **UNIQUE**——一个 human_review action 至多一个 request；
   - `request_schema_version`（当前 = `HUMAN_REVIEW_REQUEST_SCHEMA_VERSION`）、
     `request_payload` JSONB（只存 IDs + issue summaries，**不复制** Evidence
     quote / 完整 paragraph / prompt）；
   - `request_fingerprint` CHAR(64) **UNIQUE** = schema + review_action_id +
     action_fingerprint + normalized payload 的 SHA-256。

3. `human_review_decisions`——一次人工裁决，一个 request 至多一个 immutable
   decision（approve/rewrite/research/cancel）。
   - `human_decision_id` UUID PK；
   - `human_request_id` FK `human_review_requests.human_request_id` RESTRICT、
     `(human_request_id)` **UNIQUE**；
   - `decision_schema_version`（当前 = `HUMAN_REVIEW_DECISION_SCHEMA_VERSION`）、
     `decision` VARCHAR(24)（approve/rewrite/research/cancel，CHECK）、`comment`
     TEXT NULL、`decided_at` TIMESTAMPTZ（resolve 时写入）；
   - `decision_fingerprint` CHAR(64) **UNIQUE** = schema + human_request_id +
     request_fingerprint + decision + normalized comment 的 SHA-256（**不含**
     human_decision_id / decided_at / created_at）。

**downgrade guard**：三表任一存在任何行 → 拒绝回滚。ReviewAction / human
request / decision 是正式 immutable research artifact，不在 downgrade 时静默
删除历史；alembic_version 保持 0036，数据保留。三表全部为空时才允许回到 0035。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE_ACTIONS = "report_review_actions"
_TABLE_REQUESTS = "human_review_requests"
_TABLE_DECISIONS = "human_review_decisions"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE_ACTIONS,
        sa.Column(
            "review_action_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_audits.audit_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.report_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("action_schema_version", sa.Integer(), nullable=False),
        sa.Column("action_type", sa.String(24), nullable=False),
        sa.Column("action_payload", postgresql.JSONB(), nullable=False),
        sa.Column("action_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("review_action_id", name="pk_report_review_actions"),
        sa.CheckConstraint(
            "action_schema_version >= 1",
            name="ck_report_review_actions_action_schema_version",
        ),
        sa.CheckConstraint(
            "action_type IN ('finalize','rewrite','research','human_review')",
            name="ck_report_review_actions_action_type",
        ),
        sa.CheckConstraint(
            f"action_fingerprint {_SHA256_CHECK}",
            name="ck_report_review_actions_action_fingerprint",
        ),
        sa.UniqueConstraint("audit_id", name="uq_report_review_actions_audit_id"),
        sa.UniqueConstraint(
            "action_fingerprint",
            name="uq_report_review_actions_action_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE_ACTIONS}_report_id",
        _TABLE_ACTIONS,
        ["report_id"],
    )
    op.create_index(
        f"ix_{_TABLE_ACTIONS}_action_type",
        _TABLE_ACTIONS,
        ["action_type"],
    )

    op.create_table(
        _TABLE_REQUESTS,
        sa.Column(
            "human_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "review_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "report_review_actions.review_action_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("request_schema_version", sa.Integer(), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("request_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("human_request_id", name="pk_human_review_requests"),
        sa.CheckConstraint(
            "request_schema_version >= 1",
            name="ck_human_review_requests_request_schema_version",
        ),
        sa.CheckConstraint(
            f"request_fingerprint {_SHA256_CHECK}",
            name="ck_human_review_requests_request_fingerprint",
        ),
        sa.UniqueConstraint(
            "review_action_id",
            name="uq_human_review_requests_review_action_id",
        ),
        sa.UniqueConstraint(
            "request_fingerprint",
            name="uq_human_review_requests_request_fingerprint",
        ),
    )

    op.create_table(
        _TABLE_DECISIONS,
        sa.Column(
            "human_decision_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "human_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "human_review_requests.human_request_id",
                ondelete="RESTRICT",
            ),
            nullable=False,
        ),
        sa.Column("decision_schema_version", sa.Integer(), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("human_decision_id", name="pk_human_review_decisions"),
        sa.CheckConstraint(
            "decision_schema_version >= 1",
            name="ck_human_review_decisions_decision_schema_version",
        ),
        sa.CheckConstraint(
            "decision IN ('approve','rewrite','research','cancel')",
            name="ck_human_review_decisions_decision",
        ),
        sa.CheckConstraint(
            f"decision_fingerprint {_SHA256_CHECK}",
            name="ck_human_review_decisions_decision_fingerprint",
        ),
        sa.UniqueConstraint(
            "human_request_id",
            name="uq_human_review_decisions_human_request_id",
        ),
        sa.UniqueConstraint(
            "decision_fingerprint",
            name="uq_human_review_decisions_decision_fingerprint",
        ),
    )


def downgrade() -> None:
    # 数据安全：ReviewAction / human request / decision 是正式 immutable research
    # artifact，不在 downgrade 时静默删除历史。任一表存在任何行 → 拒绝回滚（不
    # 删除数据 / 不修改行），alembic_version 保持 0036。三表全部为空时才允许回
    # 到 0035。
    for table, noun in (
        (_TABLE_DECISIONS, "human review decisions"),
        (_TABLE_REQUESTS, "human review requests"),
        (_TABLE_ACTIONS, "report review actions"),
    ):
        if _table_has_row(table):
            raise RuntimeError(
                "cannot downgrade migration 0036: "
                f"rows present in {table}; refusing to drop registered "
                f"{noun} (alembic_version stays 0036)"
            )
    op.drop_table(_TABLE_DECISIONS)
    op.drop_table(_TABLE_REQUESTS)
    op.drop_table(_TABLE_ACTIONS)
