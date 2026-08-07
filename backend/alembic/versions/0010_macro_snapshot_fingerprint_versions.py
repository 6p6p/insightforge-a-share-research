"""macro snapshot fingerprint versions

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-07

阶段 2C.2B：
- macro_dataset_snapshots 增加 fingerprint_version 与 normalization_version，
  让 snapshot_fingerprint 的算法版本与解析规范版本可以直接从数据库看到。
  fingerprint_version：定义 SHA-256 canonical payload 的算法版本（当前 v1）。
  normalization_version：定义"同一原始响应如何解析成 MacroIndicator /
  MacroGeography / MacroObservation"的规范版本（当前 world_bank_v1）。
  未来解析规则改变时必须升级 normalization_version，避免同一 raw bytes +
  新 parser → 不同结构化数据 → 却命中旧 fingerprint。
- downgrade 删除两列与对应 CHECK。
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_FINGERPRINT_VERSION_CHECK = "fingerprint_version = 1"
_NORMALIZATION_VERSION_CHECK = "length(trim(normalization_version)) > 0"


def upgrade() -> None:
    op.add_column(
        "macro_dataset_snapshots",
        sa.Column(
            "fingerprint_version",
            sa.SmallInteger(),
            server_default="1",
            nullable=False,
        ),
    )
    op.add_column(
        "macro_dataset_snapshots",
        sa.Column(
            "normalization_version",
            sa.String(length=64),
            server_default="world_bank_v1",
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_macro_dataset_snapshots_fingerprint_version",
        "macro_dataset_snapshots",
        _FINGERPRINT_VERSION_CHECK,
    )
    op.create_check_constraint(
        "ck_macro_dataset_snapshots_normalization_version",
        "macro_dataset_snapshots",
        _NORMALIZATION_VERSION_CHECK,
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_macro_dataset_snapshots_normalization_version",
        "macro_dataset_snapshots",
        type_="check",
    )
    op.drop_constraint(
        "ck_macro_dataset_snapshots_fingerprint_version",
        "macro_dataset_snapshots",
        type_="check",
    )
    op.drop_column("macro_dataset_snapshots", "normalization_version")
    op.drop_column("macro_dataset_snapshots", "fingerprint_version")
