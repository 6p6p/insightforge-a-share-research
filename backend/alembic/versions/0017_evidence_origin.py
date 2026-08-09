"""evidence card origin model

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-09

阶段 3C.3A：把 EvidenceCard 泛化为双 origin（document_chunk / macro_observation），
在 Stage 4（Claim）前完成 origin 模型泛化。**不是** Macao → fake DocumentChunk：
macro Evidence 不经过 DocumentChunk / ParsedSource / Chroma / quote resolver。

设计要点：
- `origin_type` VARCHAR(32) NOT NULL DEFAULT 'document_chunk'：旧 v1 document 行
  回填 document_chunk，**不重算旧 fingerprint**（保留原样）。
- 新增 `macro_observation_id` / `macro_snapshot_id` / `macro_series_id`（UUID
  NULL，FK RESTRICT：上游存在期间不级联删除）。同一个 evidence_card_id
  namespace，不拆两个表。
- 现有 document-specific 列改为允许 NULL：source_id / parsed_source_id /
  chunk_set_id / chunk_id / quote_start / quote_end / quote_text / quote_sha256。
- conditional CHECK `ck_evidence_cards_origin_consistency`：
  - document_chunk → document provenance + quote 全 NOT NULL，macro_* 全 NULL；
  - macro_observation → macro_* 全 NOT NULL，document provenance + quote 全 NULL。
- `locator_refs` 保持 NOT NULL 且两种 origin 都非空 array：macro 存 deterministic
  structured locator（类型 macro_observation + provider/series/snapshot/
  observation identity + period），不造 fake 文本。
- provider_key / authority_tier_snapshot / critical_claim_eligible_snapshot 保持
  NOT NULL：两种 origin 都从 Source Registry / Macro provenance 确定性获得
  （macro 用 MacroDatasetSnapshot 的获取时快照，不硬编码 World Bank tier）。

downgrade：存在任何 origin_type='macro_observation' 行时拒绝回滚（恢复 document
NOT NULL 会破坏 macro 行，不静默丢失 origin semantics）；无 macro 行时 document
行满足全部 NOT NULL，可安全降级。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ORIGIN_DOCUMENT_CHUNK = "document_chunk"
_ORIGIN_MACRO_OBSERVATION = "macro_observation"

_ORIGIN_TYPE_CHECK = f"origin_type IN ('{_ORIGIN_DOCUMENT_CHUNK}','{_ORIGIN_MACRO_OBSERVATION}')"
_ORIGIN_CONSISTENCY_CHECK = f"""
(
  (origin_type = '{_ORIGIN_DOCUMENT_CHUNK}' AND
     source_id IS NOT NULL AND parsed_source_id IS NOT NULL AND
     chunk_set_id IS NOT NULL AND chunk_id IS NOT NULL AND
     quote_start IS NOT NULL AND quote_end IS NOT NULL AND
     quote_text IS NOT NULL AND quote_sha256 IS NOT NULL AND
     macro_observation_id IS NULL AND macro_snapshot_id IS NULL AND macro_series_id IS NULL)
  OR
  (origin_type = '{_ORIGIN_MACRO_OBSERVATION}' AND
     macro_observation_id IS NOT NULL AND macro_snapshot_id IS NOT NULL AND
     macro_series_id IS NOT NULL AND
     source_id IS NULL AND parsed_source_id IS NULL AND
     chunk_set_id IS NULL AND chunk_id IS NULL AND
     quote_start IS NULL AND quote_end IS NULL AND
     quote_text IS NULL AND quote_sha256 IS NULL)
)
"""


def _table_has_row(table: str, where: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} WHERE {where} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    # 1. origin_type（NOT NULL + server_default 回填旧行为 document_chunk；
    #    document path 仍显式传入，server_default 只是兜底）。
    op.add_column(
        "evidence_cards",
        sa.Column(
            "origin_type",
            sa.String(length=32),
            nullable=False,
            server_default=_ORIGIN_DOCUMENT_CHUNK,
        ),
    )
    op.create_index("ix_evidence_cards_origin_type", "evidence_cards", ["origin_type"])

    # 2. macro origin ids（NULL；FK RESTRICT，上游存在期间不级联删除）。
    op.add_column(
        "evidence_cards",
        sa.Column(
            "macro_observation_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_observations.observation_id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "evidence_cards",
        sa.Column(
            "macro_snapshot_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_dataset_snapshots.snapshot_id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.add_column(
        "evidence_cards",
        sa.Column(
            "macro_series_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("macro_series.series_id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_evidence_cards_macro_observation_id",
        "evidence_cards",
        ["macro_observation_id"],
    )

    # 3. document-specific 列改为允许 NULL（macro 行必须为 NULL）。
    for column in (
        "source_id",
        "parsed_source_id",
        "chunk_set_id",
        "chunk_id",
        "quote_start",
        "quote_end",
        "quote_text",
        "quote_sha256",
    ):
        op.alter_column("evidence_cards", column, nullable=True)

    # 4. conditional CHECK + origin 枚举 + locator 非空。
    op.create_check_constraint(
        "ck_evidence_cards_origin_type",
        "evidence_cards",
        _ORIGIN_TYPE_CHECK,
    )
    op.create_check_constraint(
        "ck_evidence_cards_origin_consistency",
        "evidence_cards",
        _ORIGIN_CONSISTENCY_CHECK,
    )
    op.create_check_constraint(
        "ck_evidence_cards_locator_refs_nonempty",
        "evidence_cards",
        "jsonb_array_length(locator_refs) > 0",
    )


def downgrade() -> None:
    # 数据安全：存在任何 macro_observation origin 行时拒绝回滚（恢复 document
    # NOT NULL 会破坏 macro 行），不静默丢失 origin semantics。
    if _table_has_row("evidence_cards", f"origin_type = '{_ORIGIN_MACRO_OBSERVATION}'"):
        raise RuntimeError(
            "cannot downgrade migration 0017: macro_observation evidence cards present; "
            "refusing to drop origin semantics silently"
        )
    op.drop_constraint("ck_evidence_cards_locator_refs_nonempty", "evidence_cards", type_="check")
    op.drop_constraint("ck_evidence_cards_origin_consistency", "evidence_cards", type_="check")
    op.drop_constraint("ck_evidence_cards_origin_type", "evidence_cards", type_="check")
    for column in (
        "source_id",
        "parsed_source_id",
        "chunk_set_id",
        "chunk_id",
        "quote_start",
        "quote_end",
        "quote_text",
        "quote_sha256",
    ):
        op.alter_column("evidence_cards", column, nullable=False)
    op.drop_index("ix_evidence_cards_macro_observation_id", table_name="evidence_cards")
    op.drop_index("ix_evidence_cards_origin_type", table_name="evidence_cards")
    op.drop_column("evidence_cards", "macro_series_id")
    op.drop_column("evidence_cards", "macro_snapshot_id")
    op.drop_column("evidence_cards", "macro_observation_id")
    op.drop_column("evidence_cards", "origin_type")
