"""Captured macro fetch contracts (stage 2C.2B).

MacroRawJsonResponse：一次成功 JSON HTTP 响应的不可变捕获，用于原始字节归档。
raw_bytes 语义：HTTPX 完成 transport/content decoding 后交给应用层的
response body bytes——不是 TCP wire bytes、不是 gzip 压缩包原始网络字节。

MacroFetchResult 保持纯领域结果，不含 raw_bytes；raw_bytes 只在
MacroRawJsonResponse 中承载，避免污染领域对象。
"""

from dataclasses import dataclass
from datetime import datetime
from re import compile as _compile

from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.macro.contracts import MacroFetchResult

# 单响应原始字节上限：与 WorldBankClient 流式读取上限一致（5 MiB）。
MACRO_MAX_JSON_RESPONSE_BYTES = 5 * 1024 * 1024

_HOSTNAME_RE = _compile(r"^[A-Za-z0-9.-]+$")


@dataclass(frozen=True)
class MacroRawJsonResponse:
    """一次成功的 JSON HTTP 响应（已捕获原始字节）。"""

    role: MacroSnapshotArtifactRole
    page: int | None
    response_status: int
    final_hostname: str
    content_type: str
    fetched_at: datetime
    raw_bytes: bytes

    def __post_init__(self) -> None:
        if self.role not in MacroSnapshotArtifactRole:
            raise ValueError(f"unknown role {self.role!r}")
        if self.role is MacroSnapshotArtifactRole.INDICATOR_METADATA:
            if self.page is not None:
                raise ValueError("indicator_metadata page must be None")
        elif self.role is MacroSnapshotArtifactRole.COUNTRY_METADATA:
            if self.page is not None:
                raise ValueError("country_metadata page must be None")
        else:  # observations_page
            if not isinstance(self.page, int) or self.page < 1:
                raise ValueError("observations_page page must be >= 1")
        if not isinstance(self.response_status, int) or not 200 <= self.response_status < 300:
            raise ValueError("response_status must be 200-299")
        if (
            not isinstance(self.final_hostname, str)
            or not self.final_hostname
            or not _HOSTNAME_RE.match(self.final_hostname)
            or "://" in self.final_hostname
        ):
            raise ValueError("final_hostname must be a bare hostname without scheme/path/query")
        media = (self.content_type or "").split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise ValueError("content_type base media type must be application/json")
        if not isinstance(self.raw_bytes, bytes) or not self.raw_bytes:
            raise ValueError("raw_bytes must be non-empty bytes")
        if len(self.raw_bytes) > MACRO_MAX_JSON_RESPONSE_BYTES:
            raise ValueError("raw_bytes exceeds limit")
        if not isinstance(self.fetched_at, datetime) or self.fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware datetime")


@dataclass(frozen=True)
class CapturedMacroFetch:
    """一次完整获取的领域结果 + 每个成功响应的原始字节捕获。

    responses 顺序由 fetch_with_capture 固定为 indicator_metadata、
    country_metadata、observations_page(page ASC)；fingerprint 计算不依赖
    tuple 顺序（Fingerprint Builder 自己排序）。
    """

    result: MacroFetchResult
    responses: tuple[MacroRawJsonResponse, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.responses, tuple):
            raise ValueError("responses must be tuple")
        for response in self.responses:
            if not isinstance(response, MacroRawJsonResponse):
                raise ValueError("responses must contain MacroRawJsonResponse")
        if not isinstance(self.result, MacroFetchResult):
            raise ValueError("result must be MacroFetchResult")
