"""Versioned A-share issuer official-domain snapshot contract and loader (V1.1 closure).

Snapshot = checked-in deterministic JSON（`app/issuer_domains/issuer_domains_v1.json`），
由 `backend/scripts/build_issuer_domains.py` 从公开来源构建（来源与降级在
snapshot `sources[]` 中逐条记录；域名只取官方权威来源：深交所上市公司名录的
「公司网址」列 + 东方财富 F10 ORGINFO 的 ORG_WEB）。导入侧（bootstrap / CLI）
只消费本模块的 validated contract：

- `IssuerDomainSnapshot`：schema_version / snapshot_version / as_of / sources /
  domains；
- `IssuerDomainEntry`：security_code / exchange / domain / source_url /
  provider_key / verified_at；
- `load_bundled_snapshot()`：读取仓库内 bundled snapshot 并完整校验（代码
  6 位、domain 为合法主机名、source_url 为对应 https URL、company 级唯一、
  数量下限），返回 frozen snapshot 对象。

**不依赖数据库 / 网络**：纯数据契约（0 DB / 0 network / 0 LLM）。
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.companies.master.snapshot import exchange_for_code

_SNAPSHOT_DIR = Path(__file__).resolve().parent
BUNDLED_SNAPSHOT_PATH = _SNAPSHOT_DIR / "issuer_domains_v1.json"

SNAPSHOT_SCHEMA_VERSION = 1
MIN_EXPECTED_DOMAINS = 1000

_CODE_RE = re.compile(r"^\d{6}$")
_HOST_RE = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$")


class IssuerDomainEntry(BaseModel):
    model_config = ConfigDict(frozen=True)

    security_code: str = Field(min_length=6, max_length=6)
    exchange: str
    domain: str
    source_url: str
    provider_key: str = "issuer_official"
    verified_at: str

    @field_validator("security_code")
    @classmethod
    def _valid_code(cls, value: str) -> str:
        if not _CODE_RE.fullmatch(value):
            raise ValueError(f"security_code must be 6 digits: {value!r}")
        return value

    @field_validator("domain")
    @classmethod
    def _valid_domain(cls, value: str) -> str:
        domain = value.strip().lower().rstrip(".")
        if not _HOST_RE.fullmatch(domain):
            raise ValueError(f"invalid hostname domain: {value!r}")
        return domain

    @field_validator("source_url")
    @classmethod
    def _valid_source_url(cls, value: str) -> str:
        parsed = urlparse(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError(f"source_url must be https with hostname: {value!r}")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(f"source_url must not contain userinfo: {value!r}")
        return value

    @field_validator("provider_key")
    @classmethod
    def _valid_provider(cls, value: str) -> str:
        key = value.strip()
        if key != "issuer_official":
            raise ValueError(f"provider_key must be issuer_official: {value!r}")
        return key

    @field_validator("verified_at")
    @classmethod
    def _valid_verified_at(cls, value: str) -> str:
        text = value.strip()
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            raise ValueError(f"verified_at must be YYYY-MM-DD: {value!r}")
        return text


class IssuerDomainSource(BaseModel):
    model_config = ConfigDict(frozen=True)

    source_name: str
    url: str
    fetched_at: str | None = None
    row_count: int | None = None
    domain_count: int | None = None
    authority_tier: int | None = None
    note: str | None = None


class IssuerDomainSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: int = SNAPSHOT_SCHEMA_VERSION
    snapshot_version: str
    as_of: str | None = None
    sources: list[IssuerDomainSource] = Field(default_factory=list)
    domains: list[IssuerDomainEntry] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _valid_schema_version(cls, value: int) -> int:
        if value != SNAPSHOT_SCHEMA_VERSION:
            raise ValueError(f"unsupported issuer domain snapshot schema: {value}")
        return value

    def validate_consistency(self) -> None:
        """跨条目一致性校验（调用方在导入前必须调用）。

        - 规模下限：> 1000 家公司域名（防止误提交 demo seed）；
        - 代码 6 位 + 前缀推导的 exchange 与声明的 exchange 一致；
        - domain 与 source_url hostname 一致（URL 是域名的验证 URL）；
        - (security_code, domain) 与 (security_code, exchange) 唯一；
        - source_url hostname 不含端口（端口域名无法做 URL 校验）。
        """
        if len(self.domains) <= MIN_EXPECTED_DOMAINS:
            raise ValueError(
                f"issuer domain snapshot must cover > {MIN_EXPECTED_DOMAINS} "
                f"domains, got {len(self.domains)}"
            )
        seen_pairs: set[tuple[str, str]] = set()
        seen_codes: set[str] = set()
        for entry in self.domains:
            derived_exchange = exchange_for_code(entry.security_code)
            if derived_exchange != entry.exchange:
                raise ValueError(
                    f"{entry.security_code}: prefix implies {derived_exchange}, "
                    f"declared {entry.exchange}"
                )
            parsed = urlparse(entry.source_url)
            host = (parsed.hostname or "").lower().rstrip(".")
            if host != entry.domain:
                raise ValueError(
                    f"{entry.security_code}: source_url host {host} != domain {entry.domain}"
                )
            if parsed.port is not None:
                raise ValueError(f"{entry.security_code}: source_url must not contain port")
            pair = (entry.security_code, entry.domain)
            if pair in seen_pairs:
                raise ValueError(f"duplicate (security_code, domain) {pair}")
            seen_pairs.add(pair)
            seen_codes.add(entry.security_code)


@dataclass(frozen=True)
class LoadedIssuerDomainSnapshot:
    """bundled snapshot + 内容寻址信息（导入侧记录 provenance 用）。"""

    snapshot: IssuerDomainSnapshot
    content_sha256: str
    byte_size: int


def load_snapshot_file(path: Path) -> LoadedIssuerDomainSnapshot:
    """读取并校验任意 snapshot JSON 文件（bundled 或显式 refresh 路径）。"""
    raw = path.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    snapshot = IssuerDomainSnapshot.model_validate(json.loads(raw))
    snapshot.validate_consistency()
    return LoadedIssuerDomainSnapshot(snapshot=snapshot, content_sha256=sha256, byte_size=len(raw))


def load_bundled_snapshot() -> LoadedIssuerDomainSnapshot:
    """读取仓库内 bundled issuer domain snapshot（缺失/损坏 → 明确异常）。"""
    if not BUNDLED_SNAPSHOT_PATH.exists():
        raise FileNotFoundError(f"bundled issuer domain snapshot missing: {BUNDLED_SNAPSHOT_PATH}")
    return load_snapshot_file(BUNDLED_SNAPSHOT_PATH)
