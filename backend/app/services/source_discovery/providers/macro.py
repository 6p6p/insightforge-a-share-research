"""Macro discovery provider (P1).

包装既有 `MacroAutoFetchService`（确定性 topic → World Bank indicator
白名单，有界 5 年窗口，幂等落库）为统一 `SourceDiscoveryProvider`。

supports：macro need 或 document 的 macro_dataset。

discover：`fetch_for_need` 成功落库 → acquired=True（observation 已落库；
evidence 卡由 MacroNeedExecutor 在重查后创建）；映射未命中 / 失败 →
exhausted（调用方保持 MACRO_DATA_UNAVAILABLE，绝不编造宏观数字）。
"""

from app.services.macro_auto_fetch_service import MacroAutoFetchService
from app.services.source_discovery.contracts import (
    REASON_NO_CANDIDATES,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)


class MacroDiscoveryProvider:
    """把 MacroAutoFetchService 包装为统一发现 Provider。"""

    provider_key = "world_bank_macro"

    def __init__(self, inner: MacroAutoFetchService | None = None) -> None:
        self._inner = inner

    def supports(self, request: SourceDiscoveryRequest) -> bool:
        if request.need_kind == "macro":
            return True
        return request.need_kind == "document" and request.source_type == "macro_dataset"

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        if self._inner is None or request.as_of is None:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        try:
            result = await self._inner.fetch_for_need(
                topic=request.topic, geo=request.geo, as_of=request.as_of
            )
        except Exception:  # noqa: BLE001 - 获取失败 → exhausted（不泄漏异常）
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        if not result.persisted:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        # observation 落库即供给成功；evidence 卡由 macro executor 创建。
        return SourceDiscoveryResult(provider_key=self.provider_key, acquired=True)
