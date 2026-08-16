"""Source discovery service (P1): provider 链编排 + 失败聚合。

`SourceDiscoveryService.discover(request)`：

1. 按 request（need_kind / source_type）筛选 `supports` 的 provider（保持注册
   顺序）；
2. 逐个 `discover`：任一 provider acquired → 立即返回成功（聚合 source_ids）；
3. 全部未 acquired → `SourceDiscoveryOutcome(exhausted=True)` + 稳定 reasons
   （调用方据此保留 SOURCE_NOT_FOUND / MACRO_DATA_UNAVAILABLE → human fallback）；
4. provider 意外异常（违反契约）→ 翻译为 exhausted + REASON_DISCOVERY_FAILED，
   **绝不向上抛**——发现失败不能阻塞编排（与 executor 既有失败语义一致）。

无 provider 匹配 → exhausted + REASON_PROVIDER_UNAVAILABLE（不伪造可用性）。
"""

from dataclasses import dataclass
from typing import Protocol

from app.core.logging import get_logger
from app.services.source_discovery.contracts import (
    REASON_DISCOVERY_FAILED,
    REASON_NO_CANDIDATES,
    REASON_PROVIDER_UNAVAILABLE,
    SourceDiscoveryOutcome,
    SourceDiscoveryProvider,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)

logger = get_logger("app.source_discovery")


class SourceDiscoveryProviderRegistry(Protocol):
    """只读 provider 集合（便于测试注入稳定列表）。"""

    def providers(self) -> list[SourceDiscoveryProvider]: ...


@dataclass(frozen=True)
class SourceDiscoveryProviders:
    """有序 provider 注册表（生产装配：announcement → macro → search/news 扩展点）。"""

    items: tuple[SourceDiscoveryProvider, ...]

    def providers(self) -> list[SourceDiscoveryProvider]:
        return list(self.items)


class SourceDiscoveryService:
    """统一发现入口：need → provider 链 → 聚合结果（0 事实编造 / 不抛确定性错误）。"""

    def __init__(
        self, providers: list[SourceDiscoveryProvider] | SourceDiscoveryProviderRegistry
    ) -> None:
        if isinstance(providers, (list, tuple)):
            self._registry: SourceDiscoveryProviderRegistry = SourceDiscoveryProviders(
                tuple(providers)
            )
        else:
            self._registry = providers

    @property
    def registry(self) -> SourceDiscoveryProviderRegistry:
        return self._registry

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryOutcome:
        reasons: list[str] = []
        acquired_ids: list = []
        matched = False
        for provider in self._registry.providers():
            try:
                if not provider.supports(request):
                    continue
            except Exception:  # noqa: BLE001 - 契约违反：不阻塞编排
                logger.warning(
                    "source_discovery_supports_failed",
                    provider=provider.provider_key,
                    need_kind=request.need_kind,
                    error_type=type(provider).__name__,
                )
                continue
            matched = True
            try:
                result: SourceDiscoveryResult = await provider.discover(request)
            except Exception as exc:  # noqa: BLE001 - 契约违反：翻译为 exhausted
                logger.warning(
                    "source_discovery_provider_error",
                    provider=provider.provider_key,
                    need_kind=request.need_kind,
                    error_type=type(exc).__name__,
                )
                reasons.append(REASON_DISCOVERY_FAILED)
                continue
            if result.acquired:
                acquired_ids.extend(result.source_ids)
                return SourceDiscoveryOutcome(
                    acquired=True,
                    exhausted=False,
                    reasons=(*reasons, None),
                    source_ids=tuple(dict.fromkeys(acquired_ids)),
                )
            reasons.append(result.reason or REASON_NO_CANDIDATES)
        if not matched:
            reasons.append(REASON_PROVIDER_UNAVAILABLE)
        return SourceDiscoveryOutcome(
            acquired=False,
            exhausted=True,
            reasons=tuple(reasons),
        )
