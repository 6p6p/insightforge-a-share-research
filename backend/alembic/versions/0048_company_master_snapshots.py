"""company master snapshot provenance schema

Revision ID: 0048
Revises: 0047
Create Date: 2026-08-14

V1.1 P0-1：Company Master production supply 的 snapshot provenance 登记。

`company_master_snapshots` 记录每一次公司主数据 bootstrap/refresh 的来源与规模
（versioned snapshot → idempotent import → provenance 留痕）：

- `snapshot_version`：snapshot 标识（如 `company-master-v1-2026-08-14`）；
- `content_sha256`：snapshot 字节的 SHA-256（内容寻址，防篡改/防重复导入）；
- `company_count` / `alias_count`：本次导入的公司与别名行数；
- `sources`：JSONB 来源清单（exchange / source_name / url / fetched_at /
  row_count / authority_tier / note——可解释、可追溯）；
- `imported_at`：导入完成时间。

**公司与别名数据不进 Alembic**：migration 只建 schema；数据经
`app/services/company_master_service.py`（启动 bootstrap）与
`app/cli/import_company_master.py`（显式 refresh）导入。

**downgrade guard**：表内存在行 → 拒绝回滚（bootstrap 历史是正式 provenance
artifact，不在 downgrade 时静默删除；alembic_version 保持 0048）。全部为空时
才允许回到 0047。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0048"
down_revision: str | None = "0047"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "company_master_snapshots"


def _table() -> sa.Table:
    return sa.Table(
        _TABLE,
        sa.MetaData(),
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("snapshot_version", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("company_count", sa.Integer(), nullable=False),
        sa.Column("alias_count", sa.Integer(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column("imported_at", sa.DateTime(timezone=True), server_default=sa.text("now()")),
    )


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("snapshot_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("snapshot_version", sa.String(length=64), nullable=False),
        sa.Column("content_sha256", sa.CHAR(length=64), nullable=False),
        sa.Column("company_count", sa.Integer(), nullable=False),
        sa.Column("alias_count", sa.Integer(), nullable=False),
        sa.Column("sources", postgresql.JSONB(), nullable=False),
        sa.Column(
            "imported_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "company_count >= 0 AND alias_count >= 0",
            name="ck_company_master_snapshots_counts",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_company_master_snapshots_sha256",
        ),
        sa.UniqueConstraint(
            "snapshot_version",
            "content_sha256",
            name="uq_company_master_snapshots_version_sha",
        ),
    )
    op.create_index(
        "ix_company_master_snapshots_version",
        _TABLE,
        ["snapshot_version"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    count = connection.execute(sa.text(f"SELECT count(*) FROM {_TABLE}")).scalar_one()
    if count:
        raise RuntimeError(
            "company master snapshot provenance rows exist; refusing silent data loss"
        )
    op.drop_index("ix_company_master_snapshots_version", table_name=_TABLE)
    op.drop_table(_TABLE)
