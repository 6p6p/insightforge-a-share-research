"""evidence bound section draft foundation

Revision ID: 0033
Revises: 0032
Create Date: 2026-08-10

阶段 5B：Evidence-bound DraftSection Writer（报告草稿单节正文）。

单表 `draft_sections` 保存 **一次已验证 ReportOutline 的单个 section** 的
不可变正文草稿。Writer 只消费 `VerifiedReportOutline`（5A Gate 1）派生的
section 输入（**只允许 Outline 允许的 Claim 及已绑定 Evidence**），经
DeepSeek V4 Flash 结构化输出（temperature=0 / thinking disabled / structured
output / 0 tools / 0 web / 0 retrieval）生成一节正文草稿。

1. `draft_section_id` UUID PK；
2. `outline_id` FK `report_outlines.outline_id` **RESTRICT**——outline 存在期间
   草稿不静默消失；草稿不可变，无 update API；
3. section 身份快照：`section_id` / `section_order` / `section_type` / `title`
   （= outline section 的值，供检索与审计，不重写）；
4. writer 身份：`section_schema_version`（当前 =
   `DRAFT_SECTION_SCHEMA_VERSION`）、`writer_name` /
   `writer_version` / `writer_model_id`（当前 =
   `evidence_bound_section_writer` / 1 / `deepseek:deepseek-v4-flash`）；
5. `writer_input_fingerprint` CHAR(64) **UNIQUE**——LLM 输入边界的确定性指纹
   （outline fingerprint + section 身份 + allowed Claim/Evidence fingerprints +
   conflict/gap 数据 + schema + writer 身份）；同输入 → replay 同一行，**0 model
   calls**；任一输入变化 → 新指纹 → 新草稿（旧行保留）；
6. `section_payload` JSONB——v1 = `{"paragraphs":[{"text":..., "claim_ids":[...],
   "evidence_card_ids":[...], "conflict_indexes":[...], "evidence_gap_indexes":[...]}]}`
   （只存真实 ID，不存 alias / prompt / raw response）；
7. `section_fingerprint` CHAR(64) **UNIQUE**——writer_input_fingerprint +
   normalized resolved payload 的 SHA-256；replay 校验用；
8. `created_at` now()。

**downgrade guard**：`draft_sections` 存在任何行 → 拒绝回滚。DraftSection 是
正式 immutable research artifact（即使可确定性重放，也不在 downgrade 时静默
删除历史）；alembic_version 保持 0033，数据保留。全部为空时才允许回到 0032。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0033"
down_revision: str | None = "0032"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "draft_sections"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "draft_section_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "outline_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_outlines.outline_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("section_id", sa.String(), nullable=False),
        sa.Column("section_order", sa.Integer(), nullable=False),
        sa.Column("section_type", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("section_schema_version", sa.Integer(), nullable=False),
        sa.Column("writer_name", sa.String(), nullable=False),
        sa.Column("writer_version", sa.Integer(), nullable=False),
        sa.Column("writer_model_id", sa.String(), nullable=False),
        sa.Column("writer_input_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column("section_payload", postgresql.JSONB(), nullable=False),
        sa.Column("section_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("draft_section_id", name="pk_draft_sections"),
        sa.CheckConstraint(
            f"writer_input_fingerprint {_SHA256_CHECK}",
            name="ck_draft_sections_writer_input_fingerprint",
        ),
        sa.CheckConstraint(
            f"section_fingerprint {_SHA256_CHECK}",
            name="ck_draft_sections_section_fingerprint",
        ),
        sa.CheckConstraint(
            "section_schema_version >= 1",
            name="ck_draft_sections_section_schema_version",
        ),
        sa.CheckConstraint("section_order >= 1", name="ck_draft_sections_section_order"),
        sa.CheckConstraint("writer_version >= 1", name="ck_draft_sections_writer_version"),
        sa.CheckConstraint(
            "btrim(section_id) <> ''",
            name="ck_draft_sections_section_id_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(title) <> ''",
            name="ck_draft_sections_title_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(writer_name) <> ''",
            name="ck_draft_sections_writer_name_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(writer_model_id) <> ''",
            name="ck_draft_sections_writer_model_id_not_blank",
        ),
        sa.UniqueConstraint(
            "writer_input_fingerprint",
            name="uq_draft_sections_writer_input_fingerprint",
        ),
        sa.UniqueConstraint(
            "section_fingerprint",
            name="uq_draft_sections_section_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE}_outline_id",
        _TABLE,
        ["outline_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_outline_section",
        _TABLE,
        ["outline_id", "section_order"],
    )


def downgrade() -> None:
    # 数据安全：DraftSection 是正式 immutable research artifact，即使可确定性
    # 重放，也不在 downgrade 时静默删除历史。存在任何行 → 拒绝回滚（不删除
    # 数据 / 不修改行），alembic_version 保持 0033。全部为空时才允许回到 0032。
    if _table_has_row(_TABLE):
        raise RuntimeError(
            "cannot downgrade migration 0033: "
            f"rows present in {_TABLE}; refusing to drop registered draft "
            "sections (alembic_version stays 0033)"
        )
    op.drop_table(_TABLE)
