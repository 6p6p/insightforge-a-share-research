"""GDELT DOC 2.0 discovery provider (stage 2D.1).

GdeltNewsDiscoveryProvider 是第一个 Discovery Provider 实现：只负责一次
HTTP 请求 + 解析候选。它不访问候选 URL、不下载正文、不写数据库、不创建
SourceRecord / Evidence，也不把 GDELT 伪装成 Tier 3 / Tier 4 SourceProvider。
"""

import httpx

from app.core.logging import get_logger
from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryQuery
from app.news.gdelt.client import GdeltDocClient
from app.news.gdelt.parser import GdeltDocParser
from app.news.provider import NewsDiscoveryResult


class GdeltNewsDiscoveryProvider:
    """GDELT DOC 2.0 发现 Provider：每次 discover 恰好一个 HTTP 请求。"""

    engine = NewsDiscoveryEngine.GDELT_DOC

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        client: GdeltDocClient | None = None,
    ) -> None:
        self._client = client or GdeltDocClient(transport=transport)

    async def discover(self, query: NewsDiscoveryQuery) -> NewsDiscoveryResult:
        captured = await self._client.discover(query)
        candidates = GdeltDocParser.parse(captured.payload)
        # HTTP 请求的 hostname/status/duration_ms 已由 client 记录；
        # Provider 在此补充 result_count（解析后的候选数）。
        get_logger("app.news.gdelt").info(
            "discovery_done",
            provider_key=self.engine.value,
            hostname=captured.raw_response.final_hostname,
            status=captured.raw_response.response_status,
            result_count=len(candidates),
            request_count=1,
        )
        return NewsDiscoveryResult(
            engine=self.engine,
            query=query,
            candidates=candidates,
            raw_response=captured.raw_response,
            fetched_at=captured.raw_response.fetched_at,
            request_count=1,
        )
