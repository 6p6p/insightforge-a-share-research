"""Source discovery layer (P1: Source Discovery Framework).

统一发现入口：missing requirement → automatic discovery → validation →
existing ingestion →（exhausted）human fallback。

- `SourceDiscoveryService`：provider 链编排 + 失败聚合；
- `SourceDiscoveryProvider`（contracts）：统一发现契约；
- providers：announcement（East Money）/ macro（World Bank）已实现；
  search（P2 LLM 挂载点）/ news（P4 GDELT 挂载点）为扩展点。
"""

from app.services.source_discovery.contracts import (
    SourceDiscoveryOutcome,
    SourceDiscoveryProvider,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)
from app.services.source_discovery.service import (
    SourceDiscoveryProviders,
    SourceDiscoveryService,
)

__all__ = [
    "SourceDiscoveryOutcome",
    "SourceDiscoveryProvider",
    "SourceDiscoveryProviders",
    "SourceDiscoveryRequest",
    "SourceDiscoveryResult",
    "SourceDiscoveryService",
]
