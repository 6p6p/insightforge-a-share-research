"""Search discovery provider integration tests (P2 Model Assisted Discovery).

真实 PostgreSQL + MockTransport fetcher + FakeQueryModel（0 真实 LLM / 0 Web）。
覆盖：

- LLM 候选命中 registry allowlist 域名 → 抓取验证 → 落库 → acquired；
- LLM 候选域名不在 allowlist → 拒绝（不抓取不落库）→ exhausted；
- 混合候选（非法 + 合法）→ 合法者落库；
- query model 失败（SearchDiscoveryUnavailable）→ exhausted；
- issuer_domains 域名 → issuer_official provider。
"""

import httpx
import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.services.source_discovery.contracts import (
    REASON_NO_CANDIDATES,
    REASON_SEARCH_NOT_CONFIGURED,
    SourceDiscoveryRequest,
)
from app.services.source_discovery.providers import SearchDiscoveryProvider
from app.services.source_discovery.search_model import (
    SearchDiscoveryOutput,
    SearchDiscoveryUnavailable,
)
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_research_planning_service import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"


class FakeQueryModel:
    """确定性 fake：固定输出 / 可注入失败 / 调用计数。"""

    def __init__(self, output: SearchDiscoveryOutput | None = None, fail_with=None) -> None:
        self._output = output or SearchDiscoveryOutput(candidates=[])
        self._fail_with = fail_with
        self.calls: list[SourceDiscoveryRequest] = []

    @property
    def model_id(self) -> str:
        return "test:fake-search-query"

    async def generate(self, request: SourceDiscoveryRequest) -> SearchDiscoveryOutput:
        self.calls.append(request)
        if self._fail_with is not None:
            raise self._fail_with
        return self._output


def _pdf_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        content=_PDF,
        headers={"content-type": "application/pdf", "content-length": str(len(_PDF))},
    )


def _request(**overrides) -> SourceDiscoveryRequest:
    base = dict(
        company_id="00000000-0000-0000-0000-000000000001",
        security_code="600519",
        need_kind="document",
        source_type="other",
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


def _provider(env, model) -> SearchDiscoveryProvider:
    from app.acquisition.http_fetcher import SafePdfFetcher

    return SearchDiscoveryProvider(
        model,
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        fetcher=SafePdfFetcher(transport=httpx.MockTransport(_pdf_handler)),
        max_bytes=1024 * 1024,
        registry_domains={"sse.com.cn": "sse", "eastmoney.com": "eastmoney"},
    )


async def _source_count(env) -> int:
    from sqlalchemy import text

    async with env["sessionmaker"]() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM source_records"))).scalar_one()
        )


async def test_allowlisted_candidate_ingested_and_acquired(env) -> None:
    """LLM 候选命中 registry allowlist → 抓取验证 → 落库 → acquired。"""
    from app.services.source_discovery.search_model import SearchCandidate

    model = FakeQueryModel(
        SearchDiscoveryOutput(
            candidates=[
                SearchCandidate(url="https://www.sse.com.cn/2024/600519.pdf", title="公司年报")
            ]
        )
    )
    provider = _provider(env, model)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is True
    assert len(outcome.source_ids) == 1
    assert await _source_count(env) == 1
    assert len(model.calls) == 1


async def test_non_allowlisted_domain_rejected(env) -> None:
    """候选域名不在 allowlist → 拒绝（不抓取不落库）→ exhausted。"""
    from app.services.source_discovery.search_model import SearchCandidate

    model = FakeQueryModel(
        SearchDiscoveryOutput(
            candidates=[SearchCandidate(url="https://evil.example.com/leak.pdf", title="未知站点")]
        )
    )
    provider = _provider(env, model)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_NO_CANDIDATES
    assert await _source_count(env) == 0


async def test_mixed_candidates_uses_allowlisted_one(env) -> None:
    """非法域名被跳过，合法域名候选落库。"""
    from app.services.source_discovery.search_model import SearchCandidate

    model = FakeQueryModel(
        SearchDiscoveryOutput(
            candidates=[
                SearchCandidate(url="https://evil.example.com/x.pdf", title="未知站点"),
                SearchCandidate(url="https://np-anotice-stock.eastmoney.com/a.pdf", title="公告"),
            ]
        )
    )
    provider = _provider(env, model)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is True
    assert len(outcome.source_ids) == 1
    assert await _source_count(env) == 1


async def test_query_model_failure_exhausted(env) -> None:
    """模型不可用 → exhausted + search_not_configured（不编造来源）。"""
    model = FakeQueryModel(fail_with=SearchDiscoveryUnavailable("down"))
    provider = _provider(env, model)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is False
    assert outcome.exhausted is True
    assert outcome.reason == REASON_SEARCH_NOT_CONFIGURED
    assert await _source_count(env) == 0


async def test_empty_candidates_exhausted(env) -> None:
    """模型无候选 → exhausted（保持 human fallback）。"""
    model = FakeQueryModel(SearchDiscoveryOutput(candidates=[]))
    provider = _provider(env, model)

    outcome = await provider.discover(_request(company_id=env["company_id"]))

    assert outcome.acquired is False
    assert outcome.exhausted is True
