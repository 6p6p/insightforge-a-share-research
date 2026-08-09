"""claims + claim_evidence_links

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-09

阶段 4A：Claim Provenance + Persistence Foundation（Source → Evidence →
**Claim** → Report → Audit 证据链中 Claim 的最小原子单元）。**不做** Report /
DraftSection / ReviewIssue / Audit；不调用 LLM；不接 Analyst Agent。

设计要点：
- `claims`：分析结论的确定性登记。caller 只能提供语义输入（research_question /
  statement / analysis_domain / claim_kind / confidence / importance /
  analyst 身份 / 三组 evidence relation ids）；company / provenance /
  authority tier / fingerprint / created_at 由 ClaimService 从真实 Evidence
  派生，不在 caller 输入内。
- `claim_fingerprint` CHAR(64) UNIQUE = canonical JSON + SHA-256（含
  claim_schema_version / company / research_question / statement / enums /
  analyst 身份 / 按 relation 分组的 ordered evidence_card_ids；不含 claim_id /
  created_at）。同一完全相同 Claim → replay 同一行；statement / evidence
  relations / confidence / analyst version 任一变化 → 新指纹 → 新行，旧行保留。
- `claim_evidence_links`：Claim ↔ EvidenceCard 的关系（supports / contradicts /
  context），**关系属于本表，不在 evidence_cards 上增加 supports_claim /
  contradicts_claim**。PK (claim_id, evidence_card_id, relation)。claim_id
  FK CASCADE（删 Claim 删 links）；evidence_card_id FK RESTRICT（证据存在期间
  link 不静默消失）。
- CHECK：analysis_domain / claim_kind / confidence / importance 枚举白名单、
  claim_schema_version >= 1、analyst_version >= 1、research_question /
  statement / analyst_name 非空白、research_question_sha256 / claim_fingerprint
  SHA-256 格式。

downgrade：claims / claim_evidence_links 有任何数据时显式拒绝回滚（不静默丢弃
Claim 证据链）；无数据时允许回到 0017。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"
_ANALYSIS_DOMAIN_CHECK = (
    "analysis_domain IN ('financial','business','event','macro','risk','valuation')"
)
_CLAIM_KIND_CHECK = "claim_kind IN ('fact','inference','risk','relative_valuation')"
_CONFIDENCE_CHECK = "confidence IN ('low','medium','high')"
_IMPORTANCE_CHECK = "importance IN ('normal','critical')"
_RELATION_CHECK = "relation IN ('supports','contradicts','context')"


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        "claims",
        sa.Column("claim_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "company_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("companies.company_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("research_question", sa.Text(), nullable=False),
        sa.Column("research_question_sha256", postgresql.CHAR(length=64), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column("analysis_domain", sa.String(length=32), nullable=False),
        sa.Column("claim_kind", sa.String(length=32), nullable=False),
        sa.Column("confidence", sa.String(length=16), nullable=False),
        sa.Column("importance", sa.String(length=16), nullable=False),
        sa.Column("analyst_name", sa.String(length=64), nullable=False),
        sa.Column("analyst_version", sa.Integer(), nullable=False),
        sa.Column("analyst_model_id", sa.String(length=200), nullable=True),
        sa.Column("claim_schema_version", sa.Integer(), nullable=False),
        sa.Column("claim_fingerprint", postgresql.CHAR(length=64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_ANALYSIS_DOMAIN_CHECK, name="ck_claims_analysis_domain"),
        sa.CheckConstraint(_CLAIM_KIND_CHECK, name="ck_claims_claim_kind"),
        sa.CheckConstraint(_CONFIDENCE_CHECK, name="ck_claims_confidence"),
        sa.CheckConstraint(_IMPORTANCE_CHECK, name="ck_claims_importance"),
        sa.CheckConstraint("claim_schema_version >= 1", name="ck_claims_claim_schema_version"),
        sa.CheckConstraint("analyst_version >= 1", name="ck_claims_analyst_version"),
        sa.CheckConstraint(
            f"research_question_sha256 {_SHA256_CHECK}",
            name="ck_claims_research_question_sha256",
        ),
        sa.CheckConstraint(
            f"claim_fingerprint {_SHA256_CHECK}", name="ck_claims_claim_fingerprint"
        ),
        sa.CheckConstraint(
            "btrim(research_question) <> ''",
            name="ck_claims_research_question_not_blank",
        ),
        sa.CheckConstraint("btrim(statement) <> ''", name="ck_claims_statement_not_blank"),
        sa.CheckConstraint("btrim(analyst_name) <> ''", name="ck_claims_analyst_name_not_blank"),
        sa.UniqueConstraint("claim_fingerprint", name="uq_claims_claim_fingerprint"),
    )
    op.create_index("ix_claims_company_id", "claims", ["company_id"])
    op.create_index("ix_claims_created_at", "claims", ["created_at"])
    op.create_index(
        "ix_claims_research_question_sha256",
        "claims",
        ["research_question_sha256"],
    )

    op.create_table(
        "claim_evidence_links",
        sa.Column(
            "claim_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("claims.claim_id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "evidence_card_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence_cards.evidence_card_id", ondelete="RESTRICT"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("relation", sa.String(length=16), primary_key=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(_RELATION_CHECK, name="ck_claim_evidence_links_relation"),
    )
    op.create_index(
        "ix_claim_evidence_links_evidence_card_id",
        "claim_evidence_links",
        ["evidence_card_id"],
    )
    op.create_index(
        "ix_claim_evidence_links_relation",
        "claim_evidence_links",
        ["relation"],
    )


def downgrade() -> None:
    # 数据安全：存在任何 Claim / Link 数据时拒绝回滚，不静默丢弃 Claim 证据链。
    if _table_has_row("claim_evidence_links", "1 = 1") or _table_has_row("claims", "1 = 1"):
        raise RuntimeError(
            "cannot downgrade migration 0018: claims/claim_evidence_links rows present; "
            "refusing to drop claim provenance silently"
        )
    op.drop_index("ix_claim_evidence_links_relation", table_name="claim_evidence_links")
    op.drop_index(
        "ix_claim_evidence_links_evidence_card_id",
        table_name="claim_evidence_links",
    )
    op.drop_table("claim_evidence_links")
    op.drop_index("ix_claims_research_question_sha256", table_name="claims")
    op.drop_index("ix_claims_created_at", table_name="claims")
    op.drop_index("ix_claims_company_id", table_name="claims")
    op.drop_table("claims")
