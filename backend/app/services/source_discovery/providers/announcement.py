"""Announcement / IR discovery provider (P1).

包装既有 `AnnouncementDiscoveryService`（East Money 受控自动发现，全部安全
边界——allowlist / %PDF 校验 / 反爬握手 / no-lookahead——由底层保证），以
统一 `SourceDiscoveryProvider` 契约接入 Source Discovery Layer。

supports：document need 的 annual_report / semiannual_report /
quarterly_report / company_announcement / issuer_ir_material。

discover：`acquire_report` 幂等落库（provider=eastmoney、
acquisition_method=automatic_discovery）；无可下载候选 / 失败 →
exhausted + REASON_NO_CANDIDATES（调用方保持 SOURCE_NOT_FOUND → human
fallback，绝不冒充来源）。
"""

from uuid import UUID

from app.services.announcement_discovery_service import AnnouncementDiscoveryService
from app.services.source_discovery.contracts import (
    REASON_NO_CANDIDATES,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)

# 支持的 document source_type（对齐 AnnouncementDiscoveryService.acquire_report）。
_SUPPORTED_SOURCE_TYPES = frozenset(
    {
        "annual_report",
        "semiannual_report",
        "quarterly_report",
        "company_announcement",
        "issuer_ir_material",
    }
)


class AnnouncementDiscoveryProvider:
    """把 AnnouncementDiscoveryService 包装为统一发现 Provider。"""

    provider_key = "eastmoney_announcement"

    def __init__(self, inner: AnnouncementDiscoveryService | None = None) -> None:
        self._inner = inner

    def supports(self, request: SourceDiscoveryRequest) -> bool:
        return (
            request.need_kind == "document"
            and request.source_type is not None
            and request.source_type in _SUPPORTED_SOURCE_TYPES
        )

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        if self._inner is None or request.as_of is None:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        try:
            result = await self._inner.acquire_report(
                company_id=str(request.company_id),
                security_code=request.security_code,
                source_type=request.source_type or "",
                period=request.period,
                as_of=request.as_of,
            )
        except Exception:  # noqa: BLE001 - 发现失败 → exhausted（不泄漏异常）
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        if result is None:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        return SourceDiscoveryResult(
            provider_key=self.provider_key,
            acquired=True,
            source_ids=(UUID(result.source_id),),
        )
