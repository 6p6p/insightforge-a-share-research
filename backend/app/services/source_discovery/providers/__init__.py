"""Discovery providers (P1): announcement / macro 已实现；search / news 扩展点。"""

from app.services.source_discovery.providers.announcement import AnnouncementDiscoveryProvider
from app.services.source_discovery.providers.macro import MacroDiscoveryProvider
from app.services.source_discovery.providers.news import NewsDiscoveryProvider
from app.services.source_discovery.providers.search import SearchDiscoveryProvider

__all__ = [
    "AnnouncementDiscoveryProvider",
    "MacroDiscoveryProvider",
    "NewsDiscoveryProvider",
    "SearchDiscoveryProvider",
]
