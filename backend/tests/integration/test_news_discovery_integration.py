"""News discovery provider integration tests (P4: GDELT pipeline 接入)。

真实 PostgreSQL + FakeGDelt + MockTransport（0 真实 GDELT / 0 Web）。覆盖：

- 事件 need：GDELT 候选 → 原创发布者验证链 → SourceRecord(news_article) →
  acquired；
- GDELT 无候选 / 验证失败 → exhausted（保持 SOURCE_NOT_FOUND）；
- 未启用（开关关闭）→ exhausted + news_not_enabled；
- 查询构造（窗口 = as_of 前 30 天，no-lookahead）。
"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryCandidate, NewsDiscoveryQuery
from app.news.provider import NewsDiscoveryResult, NewsRawDiscoveryResponse
from app.services.source_discovery.contracts import (
    REASON_NEWS_NOT_ENABLED,
    REASON_NO_CANDIDATES,
    SourceDiscoveryRequest,
)
from app.services.source_discovery.providers import NewsDiscoveryProvider
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_research_planning_service import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_AS_OF = datetime(2026, 8, 10, tzinfo=UTC)
_HTML = "<html><body><h1>测试新闻</h1></body></html>".encode()


class FakeGDelt:
    """确定性 GDELT 替身：固定候选 / 可注入失败。"""

    engine = NewsDiscoveryEngine.GDELT_DOC

    def __init__(self, candidates=(), fail_with: BaseException | None = None) -> None:
        self._candidates = candidates
        self._fail_with = fail_with
        self.queries: list[NewsDiscoveryQuery] = []

    async def discover(self, query: NewsDiscoveryQuery) -> NewsDiscoveryResult:
        self.queries.append(query)
        if self._fail_with is not None:
            raise self._fail_with
        return NewsDiscoveryResult(
            engine=self.engine,
            query=query,
            candidates=tuple(self._candidates),
            raw_response=NewsRawDiscoveryResponse(
                response_status=200,
                final_hostname="api.gdeltproject.org",
                content_type="application/json",
                raw_bytes=b"{}",
                fetched_at=_AS_OF,
            ),
            fetched_at=_AS_OF,
            request_count=1,
        )


def _candidate(url: str, title: str = "测试新闻标题") -> NewsDiscoveryCandidate:
    return NewsDiscoveryCandidate(
        rank=1,
        title=title,
        discovered_url=url,
        seen_at=_AS_OF - timedelta(days=1),
        engine=NewsDiscoveryEngine.GDELT_DOC,
    )


def _request(**overrides) -> SourceDiscoveryRequest:
    base = dict(
        company_id=uuid4(),
        security_code="600519",
        need_kind="event",
        source_type="news_article",
        as_of=_AS_OF.date(),
        research_question="分析公司重大事件",
        topic="公司事件",
    )
    base.update(overrides)
    return SourceDiscoveryRequest(**base)


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


def _html_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=_HTML,
        headers={"content-type": "text/html; charset=utf-8"},
    )


def _provider(env, gdelt, *, enabled: bool = True) -> NewsDiscoveryProvider:
    from app.acquisition.html_fetcher import SafeHtmlFetcher
    from app.services.news_original_source_service import NewsOriginalSourceService

    original_source = NewsOriginalSourceService(
        env["sessionmaker"],
        env["raw_store"],
        SafeHtmlFetcher(transport=httpx.MockTransport(_html_handler)),
    )
    return NewsDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        gdelt_provider=gdelt,
        original_source=original_source,
        enabled=enabled,
    )


async def _source_count(env) -> int:
    from sqlalchemy import text

    async with env["sessionmaker"]() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM source_records"))).scalar_one()
        )


async def test_event_need_acquired_via_gdelt_verification(env) -> None:
    """GDELT 候选 → 原创发布者验证（xinhuanet allowlist）→ SourceRecord。"""
    gdelt = FakeGDelt(
        [
            _candidate("https://www.xinhuanet.com/2026/0809/600519.shtml"),
        ]
    )
    provider = _provider(env, gdelt)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is True
    assert len(outcome.source_ids) == 1
    assert await _source_count(env) == 1
    assert len(gdelt.queries) == 1
    # 查询窗口 no-lookahead：end_at = as_of 当日 00:00 UTC。
    assert gdelt.queries[0].end_at == _AS_OF.replace(hour=0, minute=0, second=0, microsecond=0)
    assert gdelt.queries[0].start_at == gdelt.queries[0].end_at - timedelta(days=30)


async def test_no_candidates_exhausted(env) -> None:
    """GDELT 无候选 → exhausted（事件 need 保持 SOURCE_NOT_FOUND）。"""
    provider = _provider(env, FakeGDelt([]))

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_NO_CANDIDATES
    assert await _source_count(env) == 0


async def test_verification_failure_exhausted(env) -> None:
    """候选域名不在原创发布者 allowlist → 验证失败 → exhausted。"""
    gdelt = FakeGDelt([_candidate("https://evil.example.com/leak.shtml")])
    provider = _provider(env, gdelt)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert await _source_count(env) == 0


async def test_disabled_provider_exhausted(env) -> None:
    """开关关闭 → exhausted + news_not_enabled（不发起 GDELT 调用）。"""
    gdelt = FakeGDelt([_candidate("https://www.xinhuanet.com/a.shtml")])
    provider = _provider(env, gdelt, enabled=False)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_NEWS_NOT_ENABLED
    assert gdelt.queries == []
    assert await _source_count(env) == 0


async def test_document_news_need_supports(env) -> None:
    """document news_article need 同样走 news provider。"""
    gdelt = FakeGDelt([_candidate("https://www.xinhuanet.com/b.shtml")])
    provider = _provider(env, gdelt)

    outcome = await provider.discover(_request(company_id=env["company_id"], need_kind="document"))

    assert outcome.acquired is True
