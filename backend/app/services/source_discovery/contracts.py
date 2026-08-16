"""Source discovery layer contracts (P1: Source Discovery Framework).

统一发现契约：`missing requirement → automatic discovery → validation →
existing ingestion →（exhausted）human fallback`。

- `SourceDiscoveryRequest`：一次 need 的发现输入（只含语义身份 + 需要类型，
  不含内部 fingerprint / prompt / metadata）；
- `SourceDiscoveryResult`：单个 provider 的一次发现结果（acquired 表示已成功
  补供给并落库；exhausted 表示该 provider 已穷尽，没有更多候选可试）；
- `SourceDiscoveryProvider` Protocol：provider 只做**发现 + 受控落库**（经
  SourceIngestionService / 既有自动获取服务，域名 allowlist 与 provenance
  边界由底层保证），绝不生成 evidence / 数字 / 事实；
- `SourceDiscoveryService`：按 need 路由到 provider 链，聚合结果——任一
  provider acquired → 成功；全部 exhausted → 调用方保持原
  SOURCE_NOT_FOUND / MACRO_DATA_UNAVAILABLE 语义（human fallback 兜底）。

设计原则（沿用 AnnouncementDiscoveryService / MacroAutoFetchService 既有
安全边界）：
- **0 事实编造**：provider 只把真实获取的公开资料落库（%PDF 校验、allowlist、
  no-lookahead 由底层实现保证）；
- **不抛确定性错误**：provider 失败翻译为 `SourceDiscoveryResult(exhausted=...)`，
  不泄漏异常；
- **幂等**：落库走 fingerprint replay（SourceIngestionService / MacroPersistence
  既有语义）。
"""

from dataclasses import dataclass, field
from datetime import date
from typing import Protocol
from uuid import UUID

# 稳定失败 reason（供 audit / observability；不泄漏异常文本）。
REASON_PROVIDER_UNAVAILABLE = "provider_unavailable"
REASON_SEARCH_NOT_CONFIGURED = "search_not_configured"
REASON_NEWS_NOT_ENABLED = "news_not_enabled"
REASON_DISCOVERY_FAILED = "discovery_failed"
REASON_NO_CANDIDATES = "no_candidates"


@dataclass(frozen=True)
class SourceDiscoveryRequest:
    """一次 need 的发现输入（语义身份，无内部 ID-like / fingerprint）。"""

    company_id: UUID
    security_code: str
    # need 类型（document / financial / macro / event / valuation）。
    need_kind: str
    # document need 的 source_type（annual_report 等；非 document → None）。
    source_type: str | None = None
    period: str | None = None
    # no-lookahead 边界（发现窗口不得越过该日期）。
    as_of: date | None = None
    # event / 研究语义输入（用于 query 构造；非必需）。
    research_question: str | None = None
    # macro need 的 topic_or_indicator / event need 的 topic（受控短文本）。
    topic: str | None = None
    # macro need 的 geography（受控短文本；未指定由 provider 按默认处理）。
    geo: str | None = None


@dataclass(frozen=True)
class SourceDiscoveryResult:
    """单个 provider 的一次发现结果。"""

    provider_key: str
    # 已成功补供给并落库（SourceRecord / observation 等）。
    acquired: bool = False
    # acquired 时新增的 source 记录 id（非 document 供给（如 macro）可为空）。
    source_ids: tuple[UUID, ...] = ()
    # 稳定失败 reason（未 acquired 时）。
    reason: str | None = None
    # provider 内部已穷尽（无更多候选可试）。
    exhausted: bool = False


@dataclass(frozen=True)
class SourceDiscoveryOutcome:
    """provider 链的聚合结果。"""

    # 任一 provider acquired → True。
    acquired: bool = False
    # 全部 provider 已穷尽（无任何 provider 能继续供给）。
    exhausted: bool = False
    # 各 provider 的稳定 reason（保留尝试顺序）。
    reasons: tuple[str, ...] = field(default_factory=tuple)
    source_ids: tuple[UUID, ...] = field(default_factory=tuple)

    @property
    def primary_reason(self) -> str | None:
        return self.reasons[0] if self.reasons else None


class SourceDiscoveryProvider(Protocol):
    """发现 Provider 契约。

    - `provider_key`：稳定标识（registry provider_key 同构）；
    - `supports(request)`：该 provider 是否能服务此 need（确定性判定）；
    - `discover(request)`：执行受控发现 + 落库。**不抛确定性错误**——
      失败返回 `SourceDiscoveryResult(exhausted=True, reason=...)`；
      不生成 evidence / 数字 / 事实；不绕过 allowlist / no-lookahead。
    """

    provider_key: str

    def supports(self, request: SourceDiscoveryRequest) -> bool: ...

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult: ...
