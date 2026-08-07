"""SQLAlchemy model for immutable raw artifacts."""

import uuid
from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID as PgUUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# storage_key 必须为 POSIX 风格相对路径：不以 / 或 \ 开头，不含 .. 路径段。
# 注意：不用 position(chr(0) in storage_key) 检查空字节 —— PG 的 chr(0) 求值
# 会直接抛 "null character not permitted"(54000)，且 PG 文本类型天然禁止 NUL。
_STORAGE_KEY_CHECK = (
    "storage_key <> '' AND "
    "storage_key !~ '^[/\\\\]' AND "
    "storage_key !~ '(^|[/\\\\])\\.\\.([/\\\\]|$)'"
)


class RawArtifactModel(Base):
    __tablename__ = "raw_artifacts"
    __table_args__ = (
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_raw_artifacts_sha256",
        ),
        CheckConstraint(
            "byte_size > 0",
            name="ck_raw_artifacts_byte_size",
        ),
        CheckConstraint(
            "media_type = 'application/pdf'",
            name="ck_raw_artifacts_media_type",
        ),
        CheckConstraint(
            _STORAGE_KEY_CHECK,
            name="ck_raw_artifacts_storage_key",
        ),
    )

    # 主键由 Python 层 uuid4 生成，与 source_records.source_id 等业务主键一致，
    # 不使用 DB 端 gen_random_uuid() server_default。
    # 注：曾临时添加 server_default 用于排查 psycopg 空字节报错；该报错的真正
    # 根因是 storage_key CHECK 中对 chr(0) 的求值（见 ck_raw_artifacts_storage_key
    # 注释与 ADR-0009），与 UUID 参数绑定无关，故主键契约回滚为 Python uuid4。
    artifact_id: Mapped[UUID] = mapped_column(
        PgUUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    storage_key: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    byte_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    media_type: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
