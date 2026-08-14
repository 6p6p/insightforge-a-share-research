"""Versioned A-share company master snapshot contract and loader (V1.1 P0-1).

Snapshot = checked-in deterministic JSON（`app/companies/master/company_master_v1.json`），
由 `backend/scripts/build_company_master.py` 从公开权威来源构建（来源与降级在
snapshot `sources[]` 中逐条记录）。导入侧（bootstrap / CLI）只消费本模块的
validated contract：

- `CompanyMasterSnapshot`：schema_version / snapshot_version / as_of / sources /
  companies；
- `CompanyMasterEntry`：security_code / exchange / board / listing_status /
  listing_date / official_name / short_name / former_names（可选）；
- `load_bundled_snapshot()`：读取仓库内 bundled snapshot 并完整校验
  （exchange/board 一致性、代码前缀与交易所一致性、重复 identity_key、
  名称非空、数量下限），返回 frozen snapshot 对象。

**不依赖数据库 / 网络**：纯数据契约（0 DB / 0 network / 0 LLM）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

_SNAPSHOT_DIR = Path(__file__).resolve().parent
BUNDLED_SNAPSHOT_PATH = _SNAPSHOT_DIR / "company_master_v1.json"

SNAPSHOT_SCHEMA_VERSION = 1
MIN_EXPECTED_COMPANIES = 5000

_CODE_RE = re.compile(r"^\d{6}$")
_BOARDS_BY_EXCHANGE = {
    "SSE": {"sse_main", "star"},
    "SZSE": {"szse_main", "chinext"},
    "BSE": {"bse"},
}
_BOARD_PREFIX_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(688|689)"), "star"),
    (re.compile(r"^(600|601|603|605)"), "sse_main"),
    (re.compile(r"^(300|301|302)"), "chinext"),
    (re.compile(r"^(000|001|002|003)"), "szse_main"),
]
_EXCHANGE_PREFIX_RULES: list[tuple[re.Pattern, str]] = [
    (re.compile(r"^(600|601|603|605|688|689)"), "SSE"),
    (re.compile(r"^(000|001|002|003|300|301|302)"), "SZSE"),
]
_BSE_PREFIX = re.compile(r"^(4[0-9]{2}|8[0-9]{2}|920)")


def exchange_for_code(code: str) -> str | None:
    """由证券代码前缀确定性推导交易所（与构建脚本同一规则，单一事实来源）。"""
    for pattern, exchange in _EXCHANGE_PREFIX_RULES:
        if pattern.match(code):
            return exchange
    if _BSE_PREFIX.match(code):
        return "BSE"
    return None


def board_for_code(code: str, exchange: str) -> str | None:
    if exchange == "BSE":
        return "bse"
    for pattern, board in _BOARD_PREFIX_RULES:
        if pattern.match(code):
            return board
    return None


class CompanyMasterEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    security_code: str = Field(min_length=6, max_length=6)
    exchange: str
    board: str
    listing_status: str = "listed"
    listing_date: date | None = None
    official_name: str = Field(min_length=1)
    short_name: str = Field(min_length=1)
    former_names: list[str] = Field(default_factory=list)

    @field_validator("security_code")
    @classmethod
    def _valid_code(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError(f"security_code must be 6 digits: {value!r}")
        return value

    @field_validator("exchange")
    @classmethod
    def _valid_exchange(cls, value: str) -> str:
        if value not in _BOARDS_BY_EXCHANGE:
            raise ValueError(f"unknown exchange: {value!r}")
        return value

    @field_validator("listing_status")
    @classmethod
    def _valid_status(cls, value: str) -> str:
        if value not in ("listed", "delisted", "unknown"):
            raise ValueError(f"unknown listing_status: {value!r}")
        return value

    @property
    def identity_key(self) -> str:
        return f"{self.exchange}:{self.security_code}"


class CompanyMasterSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    exchange: str
    source_name: str
    url: str
    fetched_at: str | None = None
    row_count: int | None = None
    skipped: dict[str, int] | None = None
    authority_tier: int | None = None
    note: str | None = None


class CompanyMasterSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    snapshot_version: str
    as_of: str | None = None
    sources: list[CompanyMasterSource] = Field(default_factory=list)
    companies: list[CompanyMasterEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _valid_schema_version(cls, value: int) -> int:
        if value != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unsupported company master snapshot schema: {value}")
        return value

    def validate_consistency(self) -> None:
        """跨条目一致性校验（调用方在导入前必须调用）。

        - 规模下限：完整 A 股主数据 snapshot 必须覆盖当前上市公司规模
          （> 5000），防止误提交 demo seed；
        - 每家公司：代码前缀推导的 exchange/board 与声明的 exchange/board 一致；
        - identity_key 全局唯一；(exchange, security_code) 全局唯一；
        - 名称非空；former_names 非空字符串。
        """
        if len(self.companies) <= MIN_EXPECTED_COMPANIES:
            raise ValueError(
                f"company master snapshot must cover > {MIN_EXPECTED_COMPANIES} "
                f"companies, got {len(self.companies)}"
            )
        seen_keys: set[str] = set()
        seen_codes: set[str] = set()
        for entry in self.companies:
            derived_exchange = exchange_for_code(entry.security_code)
            if derived_exchange != entry.exchange:
                raise ValueError(
                    f"{entry.security_code}: prefix implies {derived_exchange}, "
                    f"declared {entry.exchange}"
                )
            derived_board = board_for_code(entry.security_code, entry.exchange)
            if derived_board != entry.board:
                raise ValueError(
                    f"{entry.security_code}: board {entry.board} inconsistent with prefix"
                )
            if entry.identity_key in seen_keys:
                raise ValueError(f"duplicate identity_key {entry.identity_key}")
            seen_keys.add(entry.identity_key)
            if (entry.exchange, entry.security_code) in seen_codes:
                raise ValueError(f"duplicate (exchange, code) {entry.security_code}")
            seen_codes.add((entry.exchange, entry.security_code))
            if not entry.official_name.strip() or not entry.short_name.strip():
                raise ValueError(f"{entry.security_code}: empty name")
            for former in entry.former_names:
                if not former.strip():
                    raise ValueError(f"{entry.security_code}: empty former name")


@dataclass(frozen=True)
class LoadedSnapshot:
    """bundled snapshot + 内容寻址信息（导入侧记录 provenance 用）。"""

    snapshot: CompanyMasterSnapshot
    content_sha256: str
    byte_size: int


def load_snapshot_file(path: Path) -> LoadedSnapshot:
    """读取并校验任意 snapshot JSON 文件（bundled 或显式 refresh 路径）。"""
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    snapshot = CompanyMasterSnapshot.model_validate(json.loads(raw))
    snapshot.validate_consistency()
    return LoadedSnapshot(snapshot=snapshot, content_sha256=sha256, byte_size=len(raw))


def load_bundled_snapshot() -> LoadedSnapshot:
    """读取仓库内 bundled company master snapshot（缺失/损坏 → 明确异常）。"""
    if not BUNDLED_SNAPSHOT_PATH.exists():
        raise FileNotFoundError(f"bundled company master snapshot missing: {BUNDLED_SNAPSHOT_PATH}")
    return load_snapshot_file(BUNDLED_SNAPSHOT_PATH)
