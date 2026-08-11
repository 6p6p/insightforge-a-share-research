"""deterministic report export foundation

Revision ID: 0039
Revises: 0038
Create Date: 2026-08-11

阶段 6C：Deterministic Export。单表 `report_exports`。

- `export_id` UUID PK；
- `task_id` FK `research_tasks.task_id` RESTRICT、`report_id` FK
  `reports.report_id` RESTRICT——上游存在期间 Export 不静默消失；Export 不可变，
  无 update API（同输入 → replay 同一行）；
- `check_result_id` FK `report_check_results.check_result_id` RESTRICT、
  `audit_id` FK `report_audits.audit_id` RESTRICT——导出绑定到**它自己引用的**
  check / audit，`verify_export_integrity` 按这些行独立重验，不依赖 task 的
  canonical lineage 是否变化；`human_decision_id` FK
  `human_review_decisions.human_decision_id` RESTRICT **NULL**（audit pass 路径）
  或非空（人工批准路径）；
- `export_schema_version` INTEGER >=1；`export_format` VARCHAR(16)
  （markdown/docx/pdf，CHECK）；`export_input_fingerprint` CHAR(64) **UNIQUE**
  （export schema + task + report/check/audit/decision 指纹 + format +
  renderer 身份 + normalized pack 身份 的 SHA-256，**并发唯一性来源**，同输入 →
  replay 同一行）；
- `content_sha256` CHAR(64) / `byte_size` BIGINT >0 / `media_type` VARCHAR(128) /
  `file_name` VARCHAR(255) / `storage_key` TEXT——归档字节的内容寻址描述；
- `created_at` now()。

**downgrade guard**：`report_exports` 存在任何行 → 拒绝回滚。Export 是正式
immutable research artifact（即使可确定性重放，也不在 downgrade 时静默删除
历史）；alembic_version 保持 0039。表为空时才允许回到 0038。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0039"
down_revision: str | None = "0038"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "report_exports"

_SHA256_CHECK = "~ '^[0-9a-f]{64}$'"


def _table_has_row(table: str) -> bool:
    bind = op.get_bind()
    rows = bind.execute(sa.text(f"SELECT 1 FROM {table} LIMIT 1"))
    return rows.first() is not None


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "export_id",
            postgresql.UUID(as_uuid=True),
            nullable=False,
        ),
        sa.Column(
            "task_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_tasks.task_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("reports.report_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "check_result_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_check_results.check_result_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "audit_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report_audits.audit_id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "human_decision_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey(
                "human_review_decisions.human_decision_id",
                ondelete="RESTRICT",
            ),
            nullable=True,
        ),
        sa.Column("export_schema_version", sa.Integer(), nullable=False),
        sa.Column("export_format", sa.String(16), nullable=False),
        sa.Column("export_input_fingerprint", postgresql.CHAR(64), nullable=False),
        sa.Column("content_sha256", postgresql.CHAR(64), nullable=False),
        sa.Column("byte_size", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(128), nullable=False),
        sa.Column("file_name", sa.String(255), nullable=False),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("export_id", name="pk_report_exports"),
        sa.CheckConstraint(
            "export_schema_version >= 1",
            name="ck_report_exports_export_schema_version",
        ),
        sa.CheckConstraint(
            "export_format IN ('markdown','docx','pdf')",
            name="ck_report_exports_export_format",
        ),
        sa.CheckConstraint(
            f"export_input_fingerprint {_SHA256_CHECK}",
            name="ck_report_exports_export_input_fingerprint",
        ),
        sa.CheckConstraint(
            f"content_sha256 {_SHA256_CHECK}",
            name="ck_report_exports_content_sha256",
        ),
        sa.CheckConstraint(
            "byte_size > 0",
            name="ck_report_exports_byte_size",
        ),
        sa.UniqueConstraint(
            "export_input_fingerprint",
            name="uq_report_exports_export_input_fingerprint",
        ),
    )
    op.create_index(
        f"ix_{_TABLE}_task_id",
        _TABLE,
        ["task_id"],
    )
    op.create_index(
        f"ix_{_TABLE}_report_id",
        _TABLE,
        ["report_id"],
    )


def downgrade() -> None:
    # 数据安全：Export 是正式 immutable research artifact，即使可确定性重放，
    # 也不在 downgrade 时静默删除历史。表存在任何行 → 拒绝回滚（不删除数据 /
    # 不修改行），alembic_version 保持 0039。表为空时才允许回到 0038。
    if _table_has_row(_TABLE):
        raise RuntimeError(
            "cannot downgrade migration 0039: "
            f"rows present in {_TABLE}; refusing to drop registered "
            "report exports (alembic_version stays 0039)"
        )
    op.drop_table(_TABLE)
