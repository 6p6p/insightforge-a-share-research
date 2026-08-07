"""Generic news discovery provider protocol (stage 2D.1).

NewsDiscoveryProvider 只负责"发现候选 URL"，不访问候选、不下载正文、
不写数据库、不创建 SourceRecord / Evidence。raw_bytes 只存在于
NewsRawDiscoveryResponse，绝不塞进 NewsDiscoveryCandidate。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol, runtime_checkable

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryCandidate, NewsDiscoveryQuery


@dataclass(frozen=True)
class NewsRawDiscoveryResponse:
    """一次发现请求的原始响应捕获（原始字节，供归档为 RawArtifact）。"""

    response_status: int
    final_hostname: str
    content_type: str
    raw_bytes: bytes
    fetched_at: datetime


@dataclass(frozen=True)
class NewsDiscoveryResult:
    """一次发现查询的结果：engine + query + 排序后的候选 + 原始响应捕获。

    - candidates 按 rank 升序稳定排序；
    - request_count 当前固定 1（单次请求）；
    - 本结果不是 Evidence，也不代表任何原始新闻已验证。
    """

    engine: NewsDiscoveryEngine
    query: NewsDiscoveryQuery
    candidates: tuple[NewsDiscoveryCandidate, ...]
    raw_response: NewsRawDiscoveryResponse
    fetched_at: datetime
    request_count: int


@runtime_checkable
class NewsDiscoveryProvider(Protocol):
    """Discovery Provider 协议：给定查询返回候选与原始响应捕获。"""

    engine: NewsDiscoveryEngine

    async def discover(self, query: NewsDiscoveryQuery) -> NewsDiscoveryResult: ...
