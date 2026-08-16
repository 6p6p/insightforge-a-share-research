"""financial extraction evidence origin schema (Final Autonomous Research)

Revision ID: 0050
Revises: 0049
Create Date: 2026-08-16

`evidence_cards.origin_type` 增加 `financial_extraction`：自动财务提取
（deterministic，0 LLM）产生的证据卡——quote 是 ParsedSourceBlock 文本的
逐字切片（含精确数字 token），source 是原始报告 SourceRecord（tier 快照
继承报告来源，不硬编码）；locator = structured financial_extraction
locator（block_id + page/line）。

**downgrade guard**：存在 financial_extraction 卡时拒绝回滚（正式
provenance 不静默删除）。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0050"
down_revision: str | None = "0049"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_TYPE_CHECK = (
    "origin_type IN ('document_chunk','macro_observation','user_supplied','financial_extraction')"
)
_ORIGIN_CONSISTENCY_CHECK = """
(
  (origin_type = 'document_chunk' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     chunk_set_id IS NOT NULL AND chunk_id IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = 'macro_observation' AND
     macro_observation_id IS NOT NULL AND macro_snapshot_id IS NOT NULL AND
     macro_series_id IS NOT NULL AND
     source_id IS NULL AND parsed_source_id IS NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     quote_text IS NULL AND quote_sha256 IS NULL)
  OR
  (origin_type = 'user_supplied' AND
     source_id IS NOT NULL AND quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     parsed_source_id IS NULL AND chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = 'financial_extraction' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
)
"""
_LEGACY_ORIGIN_CONSISTENCY_CHECK = """
(
  (origin_type = 'document_chunk' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     chunk_set_id IS NOT NULL AND chunk_id IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = 'macro_observation' AND
     macro_observation_id IS NOT NULL AND macro_snapshot_id IS NOT NULL AND
     macro_series_id IS NOT NULL AND
     source_id IS NULL AND parsed_source_id IS NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     quote_text IS NULL AND quote_sha256 IS NULL)
  OR
  (origin_type = 'user_supplied' AND
     source_id IS NOT NULL AND quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     parsed_source_id IS NULL AND chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
)
"""


def upgrade() -> None:
    op.drop_constraint("ck_evidence_cards_origin_type", "evidence_cards", type_="check")
    op.drop_constraint("ck_evidence_cards_origin_consistency", "evidence_cards", type_="check")
    op.create_check_constraint(
        "ck_evidence_cards_origin_type", "evidence_cards", _ORIGIN_TYPE_CHECK
    )
    op.create_check_constraint(
        "ck_evidence_cards_origin_consistency", "evidence_cards", _ORIGIN_CONSISTENCY_CHECK
    )


def downgrade() -> None:
    conn = op.get_bind()
    count = conn.execute(
        sa.text("SELECT count(*) FROM evidence_cards WHERE origin_type = 'financial_extraction'")
    ).scalar_one()
    if count > 0:
        raise RuntimeError("downgrade blocked: financial_extraction evidence cards exist")
    op.drop_constraint("ck_evidence_cards_origin_type", "evidence_cards", type_="check")
    op.drop_constraint("ck_evidence_cards_origin_consistency", "evidence_cards", type_="check")
    op.create_check_constraint(
        "ck_evidence_cards_origin_type",
        "evidence_cards",
        "origin_type IN ('document_chunk','macro_observation','user_supplied')",
    )
    op.create_check_constraint(
        "ck_evidence_cards_origin_consistency",
        "evidence_cards",
        _LEGACY_ORIGIN_CONSISTENCY_CHECK,
    )
