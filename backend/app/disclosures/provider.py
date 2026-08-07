"""Disclosure provider surface.

本模块只重新导出契约与 Protocol，不提供抽象父类：
Provider 通过实现 `DisclosureDiscoveryProvider` 协议接入，避免继承层级。
"""

from app.disclosures.contracts import (
    DisclosureCandidate,
    DisclosureDiscoveryProvider,
    DisclosureSearchRequest,
)

__all__ = [
    "DisclosureCandidate",
    "DisclosureDiscoveryProvider",
    "DisclosureSearchRequest",
]
