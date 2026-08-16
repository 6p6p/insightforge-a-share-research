"""Discovery providers (P1/P3/P4): announcement / macro / search / ir / news。"""

from app.services.source_discovery.providers.announcement import AnnouncementDiscoveryProvider
from app.services.source_discovery.providers.ir import IrDiscoveryProvider
from app.services.source_discovery.providers.macro import MacroDiscoveryProvider
from app.services.source_discovery.providers.news import NewsDiscoveryProvider
from app.services.source_discovery.providers.search import SearchDiscoveryProvider

__all__ = [
    "AnnouncementDiscoveryProvider",
    "IrDiscoveryProvider",
    "MacroDiscoveryProvider",
    "NewsDiscoveryProvider",
    "SearchDiscoveryProvider",
]
