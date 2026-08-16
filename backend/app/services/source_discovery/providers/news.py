"""News discovery provider (P4: GDELT pipeline 接入)。

复用既有 news 管线：

    NewsDiscoveryQuery → GdeltNewsDiscoveryProvider（发现候选）
        → NewsDiscoveryPersistenceService（候选落库，fingerprint 幂等）
        → NewsOriginalSourceService.verify_candidate（原创发布者验证链：
          Resolver + SafeHtmlFetcher → RawArtifact → SourceRecord(news_article)）

- 查询构造确定性（research_question + topic 模板；时间窗 = as_of 前 30 天，
  no-lookahead：end_at = as_of 当日 00:00）；
- 候选逐个验证，第一个成功落库即 acquired；全部失败 → exhausted（事件 need
  保持 SOURCE_NOT_FOUND → human fallback，绝不冒充来源）；
- `news_discovery_enabled` 开关（默认关闭——GDELT 真实网络调用需显式启用）；
- 不生成 evidence / 数字 / 事实；验证链不变量（published_at 恒 NULL，
  no-lookahead 由 acquired_at 承担）保持不变。
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.acquisition.html_fetcher import SafeHtmlFetcher
from app.news.contracts import NewsDiscoveryQuery
from app.repositories.news_discovery_candidate_repository import (
    NewsDiscoveryCandidateRepository,
)
from app.services.news_discovery_service import NewsDiscoveryPersistenceService
from app.services.news_original_source_service import NewsOriginalSourceService
from app.services.source_discovery.contracts import (
    REASON_NEWS_NOT_ENABLED,
    REASON_NO_CANDIDATES,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)
from app.storage.raw_store import LocalRawArtifactStore

# 事件时效窗口（as_of 前 30 天；有界，不无限回溯）。
_NEWS_WINDOW_DAYS = 30
_MAX_NEWS_RESULTS = 20


class NewsDiscoveryProvider:
    """新闻/事件发现（P4）：GDELT 候选 → 原创发布者验证 → SourceRecord。"""

    provider_key = "news_discovery"

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker | None = None,
        raw_store: LocalRawArtifactStore | None = None,
        gdelt_provider=None,
        persistence=None,
        original_source=None,
        enabled: bool = True,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._gdelt_provider = gdelt_provider
        self._persistence = persistence
        self._original_source = original_source
        self._enabled = enabled

    def supports(self, request: SourceDiscoveryRequest) -> bool:
        return request.need_kind == "event" or (
            request.need_kind == "document" and request.source_type == "news_article"
        )

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        if not self._enabled or self._gdelt_provider is None:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NEWS_NOT_ENABLED,
                exhausted=True,
            )
        if request.as_of is None or self._sessionmaker is None or self._raw_store is None:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        try:
            query = self._build_query(request)
            persistence = self._persistence or NewsDiscoveryPersistenceService(
                self._sessionmaker, self._raw_store
            )
            original_source = self._original_source or NewsOriginalSourceService(
                self._sessionmaker, self._raw_store, SafeHtmlFetcher()
            )
            run = await persistence.discover_and_persist(self._gdelt_provider, query)
            async with self._sessionmaker() as session:
                candidates = await NewsDiscoveryCandidateRepository(session).list_for_run(
                    run.discovery_run_id
                )
            for candidate in candidates:
                try:
                    verified = await original_source.verify_candidate(candidate.candidate_id)
                except Exception:  # noqa: BLE001 - 单个候选验证失败 → 尝试下一个
                    continue
                if verified.replayed:
                    continue  # 已存在相同来源 → 尝试下一个候选
                return SourceDiscoveryResult(
                    provider_key=self.provider_key,
                    acquired=True,
                    source_ids=(verified.source_id,),
                )
        except Exception:  # noqa: BLE001 - 发现链失败 → exhausted（不泄漏异常）
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        return SourceDiscoveryResult(
            provider_key=self.provider_key,
            acquired=False,
            reason=REASON_NO_CANDIDATES,
            exhausted=True,
        )

    # ------------------------------------------------------------ internal

    @staticmethod
    def _build_query(request: SourceDiscoveryRequest) -> NewsDiscoveryQuery:
        """确定性查询构造：research_question + topic；窗口 = as_of 前 30 天。"""
        end_at = datetime.combine(request.as_of, datetime.min.time(), tzinfo=UTC)
        start_at = end_at - timedelta(days=_NEWS_WINDOW_DAYS)
        query_text = " ".join(
            part for part in (request.research_question, request.topic) if part
        ).strip()
        if not query_text:
            query_text = request.security_code
        return NewsDiscoveryQuery(
            company_id=request.company_id,
            query_text=query_text[:300],
            start_at=start_at,
            end_at=end_at,
            max_results=_MAX_NEWS_RESULTS,
        )
