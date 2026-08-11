"""research backflow contract foundation (stage 5E.2B)

Revision ID: 0038
Revises: 0037
Create Date: 2026-08-11

阶段 5E.2B：Research Backflow Contract（可验证的研究交接 / 续跑）。

两表：
1. `research_backflow_requests`——一次 **research_required** Stage 5 run 的
   可验证研究交接请求（immutable link：source Stage 5 run + 裁决后的 review
   action [± human decision] + source Report → 确定性身份 / cutoff 绑定 +
   structured 交接 payload）：

   - `research_request_id` UUID PK；
   - `source_stage5_run_id` FK `workflow_runs.run_id` RESTRICT、**UNIQUE**
     （一个 Stage 5 run 至多一个 research 请求；并发同 run → 最终 1 行）；
   - `review_action_id` FK `report_review_actions.review_action_id` RESTRICT
     NOT NULL——legal trigger 只能是 research action（无 human decision）或
     human_review action（有 research decision），服务层保证；
   - `human_decision_id` FK `human_review_decisions.human_decision_id`
     RESTRICT NULL（direct research 为 NULL，human research 非空）；
   - `source_report_id` FK `reports.report_id` RESTRICT NOT NULL——身份 /
     cutoff 从 Report → Outline → Synthesis chain 恢复；
   - `company_id` FK `companies.company_id` RESTRICT NOT NULL、
     `research_question_sha256` CHAR(64) NOT NULL、`analysis_as_of` DATE
     NOT NULL——确定性绑定，caller 不能提供；
   - `request_schema_version` INTEGER（当前 =
     `RESEARCH_BACKFLOW_REQUEST_SCHEMA_VERSION`）、`request_fingerprint`
     CHAR(64) **UNIQUE**（schema + source run + action [± decision] + report
     + 身份/cutoff + normalized payload 的 SHA-256，同 run → replay 同一行，
     不含 research_request_id / created_at）；
   - `request_payload` JSONB（结构化交接：review_issue_ids /
     target_section_ids / related_claim_ids / related_evidence_card_ids /
     research_need_codes，**不写长 prose / 不自动生成 query**）；
   - `created_at` now()。

2. `research_backflow_fulfillments`——upstream（Stage 2/3/4）返回新
   SynthesisResult 后的 fulfillment（immutable link）：

   - `fulfillment_id` UUID PK；
   - `research_request_id` FK `research_backflow_requests.research_request_id`
     RESTRICT、**UNIQUE**（一个请求至多一个 fulfillment；不同结果不覆盖 →
     `ResearchBackflowAlreadyFulfilled`）；
   - `new_synthesis_result_id` FK `claim_synthesis_results.synthesis_result_id`
     RESTRICT NOT NULL（同一 company / research-question / cutoff，且不是
     source synthesis——no-progress 政策服务层保证）；
   - `fulfillment_schema_version` INTEGER、`fulfillment_fingerprint` CHAR(64)
     **UNIQUE**（schema + request id+fingerprint + new synthesis result
     id+result fingerprint + new synthesis run id+synthesis fingerprint）；
   - `created_at` now()。

**downgrade guard**：存在任何行（请求或 fulfillment）→ 拒绝回滚。Backflow 是
正式 immutable research artifact（记录了裁决后的一次研究交接 / 交接兑现），不在
downgrade 时静默删除历史；alembic_version 保持 0038，数据保留。两表全部为空时才
允许回到 0037。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0038"
down_revision: str | None = "0037"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_REQUESTS = "research_backflow_requests"
_FULFILLMENTS = "research_backflow_fulfillments"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _REQUESTS,
        sa.Column(
            "research_request_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "source_stage5_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workflow_runs.run_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "review_action_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_review_actions.review_action_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "human_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("human_review_decisions.human_decision_id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column(
            "source_report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.report_id", ondelete="RESTRICT"),
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
        sa.Column("request_schema_version", sa.Integer(), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("request_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("research_request_id", name="pk_research_backflow_requests"),
        sa.CheckConstraint(
            "request_schema_version >= 1",
            name="ck_research_backflow_requests_request_schema_version",
        ),
        sa.CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_research_backflow_requests_research_question_sha256",
        ),
        sa.CheckConstraint(
            f"request_fingerprint {_SHA256_CHECK}",
            name="ck_research_backflow_requests_request_fingerprint",
        ),
        sa.UniqueConstraint(
            "source_stage5_run_id",
            name="uq_research_backflow_requests_source_stage5_run_id",
        ),
        sa.UniqueConstraint(
            "request_fingerprint",
            name="uq_research_backflow_requests_request_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_REQUESTS}_review_action_id",
        _REQUESTS,
        ["review_action_id"],
    )
    op.create_index(
        f"ix_{_REQUESTS}_human_decision_id",
        _REQUESTS,
        ["human_decision_id"],
    )
    op.create_index(
        f"ix_{_REQUESTS}_source_report_id",
        _REQUESTS,
        ["source_report_id"],
    )
    op.create_index(
        f"ix_{_REQUESTS}_company_id",
        _REQUESTS,
        ["company_id"],
    )

    op.create_table(
        _FULFILLMENTS,
        sa.Column(
            "fulfillment_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "research_request_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_backflow_requests.research_request_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "new_synthesis_result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claim_synthesis_results.synthesis_result_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("fulfillment_schema_version", sa.Integer(), nullable=False),
        sa.Column("fulfillment_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("fulfillment_id", name="pk_research_backflow_fulfillments"),
        sa.CheckConstraint(
            "fulfillment_schema_version >= 1",
            name="ck_research_backflow_fulfillments_fulfillment_schema_version",
        ),
        sa.CheckConstraint(
            f"fulfillment_fingerprint {_SHA256_CHECK}",
            name="ck_research_backflow_fulfillments_fulfillment_fingerprint",
        ),
        sa.UniqueConstraint(
            "research_request_id",
            name="uq_research_backflow_fulfillments_research_request_id",
        ),
        sa.UniqueConstraint(
            "fulfillment_fingerprint",
            name="uq_research_backflow_fulfillments_fulfillment_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_FULFILLMENTS}_new_synthesis_result_id",
        _FULFILLMENTS,
        ["new_synthesis_result_id"],
    )


def downgrade() -> None:
    # 数据安全：Backflow 是正式 immutable research artifact（记录了裁决后的一次研究
    # 交接 / 交接兑现），不在 downgrade 时静默删除历史。任一表存在任何行 → 拒绝回滚
    # （不删除数据 / 不修改行），alembic_version 保持 0038。两表全部为空时才允许回到
    # 0037。
    if _table_has_row(_FULFILLMENTS) or _table_has_row(_REQUESTS):
        raise RuntimeError(
            "cannot downgrade migration 0038: rows present in "
            f"{_REQUESTS}/{_FULFILLMENTS}; refusing to drop registered research "
            "backflow requests/fulfillments (alembic_version stays 0038)"
        )
    op.drop_table(_FULFILLMENTS)
    op.drop_table(_REQUESTS)
