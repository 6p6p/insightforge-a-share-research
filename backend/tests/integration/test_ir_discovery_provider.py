"""IrDiscoveryProvider integration tests (P3: Company Website/IR Discovery).

真实 PostgreSQL：issuer_domains 登记域名 → 有界爬取（注入 Fake fetcher，
0 网络）→ issuer_ir_material SourceRecord 落库（provider=issuer_official、
Tier-2、published_at NULL）。
"""

import io
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.services.source_discovery.contracts import SourceDiscoveryRequest
from app.services.source_discovery.providers import IrDiscoveryProvider
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_evidence_card_service import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_HOME = """<html><head><title>宁德时代官网</title></head><body>
<a href="/investor">投资者关系</a>
<a href="/investor/relations">投资者关系2</a>
<a href="https://www.catl.com/investor/annual.pdf">年报PDF</a>
<a href="https://www.catl.com/careers">招聘</a>
<a href="https://other-domain.com/x">外域</a>
<a href="http://www.catl.com/plain">http降级</a>
<a href="#frag">fragment</a>
</body></html>""".encode()
_IR_PAGE = """<html><head><title>投资者关系</title></head><body>
<a href="/investor/notice">公告</a>
<a href="/investor/summary.pdf">业绩说明会</a>
</body></html>""".encode()
_CAREERS = """<html><head><title>招聘</title></head><body><p>职业发展</p></body></html>""".encode()


class _FakeHtmlFetcher:
    """按 URL 返回固定页面（0 网络）；记录调用。"""

    def __init__(self, pages: dict) -> None:
        self._pages = pages
        self.calls: list[str] = []

    async def fetch(self, url: str, provider_key: str, allowed_domains: list[str]):
        self.calls.append(url)
        if url not in self._pages:
            raise RuntimeError("page not found")
        page = self._pages[url]
        from app.acquisition.html_fetcher import FetchedHtmlPage

        return FetchedHtmlPage(
            requested_url=url,
            final_url=page[0],
            final_hostname="catl.com",
            status_code=200,
            content_type="text/html",
            redirect_count=0,
            fetched_at=datetime.now(UTC),
            raw_bytes=page[1],
        )


class _FakePdfFetcher:
    """固定 PDF 内容（%PDF 开头）；记录调用。"""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def fetch(self, url: str, allowed_domains: list[str], max_bytes: int):
        self.calls.append(url)
        from app.acquisition.http_fetcher import FetchedPdf

        return FetchedPdf(
            final_url=url,
            content_stream=io.BytesIO(b"%PDF-1.4 fake content"),
            tmp_path="",
            reported_content_type="application/pdf",
            reported_content_length=20,
        )


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
    company_id = await _seed_company(sessionmaker, "300750")
    # 登记官网域名（issuer_domains registry 行）。
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO issuer_domains (domain_id, company_id, domain, source_url, "
                "provider_key, verified_at, created_at) "
                "VALUES (:id, :cid, 'catl.com', 'https://www.catl.com', 'snapshot', :v, :c)"
            ),
            {
                "id": uuid4(),
                "cid": company_id,
                "v": datetime.now(UTC),
                "c": datetime.now(UTC),
            },
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


def _request(env: dict) -> SourceDiscoveryRequest:
    return SourceDiscoveryRequest(
        company_id=env["company_id"],
        security_code="300750",
        need_kind="document",
        source_type="issuer_ir_material",
        as_of=date(2026, 8, 10),
        topic="投资者关系",
    )


async def test_supports_only_ir_material(env) -> None:
    provider = IrDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        html_fetcher=_FakeHtmlFetcher({}),
    )
    assert provider.supports(_request(env))
    assert not provider.supports(
        SourceDiscoveryRequest(
            company_id=env["company_id"],
            security_code="300750",
            need_kind="document",
            source_type="annual_report",
            as_of=date(2026, 8, 10),
        )
    )
    assert not provider.supports(
        SourceDiscoveryRequest(
            company_id=env["company_id"],
            security_code="300750",
            need_kind="event",
            source_type=None,
            as_of=date(2026, 8, 10),
        )
    )


async def test_discover_no_domains_exhausted(env) -> None:
    async with env["sessionmaker"]() as session:
        await session.execute(text("DELETE FROM issuer_domains"))
        await session.commit()
    provider = IrDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        html_fetcher=_FakeHtmlFetcher({}),
    )
    result = await provider.discover(_request(env))
    assert not result.acquired
    assert result.exhausted
    assert result.reason == "no_candidates"


async def test_discover_crawls_and_persists_html_and_pdf(env) -> None:
    """首页 → IR 栏目 → PDF 材料落库；published_at 恒 NULL；仅同 host https。"""
    html = _FakeHtmlFetcher(
        {
            "https://catl.com/": ("https://catl.com/", _HOME),
            "https://catl.com/investor": ("https://catl.com/investor", _IR_PAGE),
        }
    )
    pdf = _FakePdfFetcher()
    provider = IrDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        html_fetcher=html,
        pdf_fetcher=pdf,
    )
    result = await provider.discover(_request(env))

    assert result.acquired
    assert len(result.source_ids) >= 2
    # 有界：不会抓 careers（IR 关键词排序 + 页数上限内仍可能抓取——至少不越界）。
    async with env["sessionmaker"]() as session:
        rows = (
            (
                await session.execute(
                    text(
                        "SELECT document_type, provider_key, published_at, "
                        "authority_tier_snapshot, critical_claim_eligible_snapshot, "
                        "acquisition_method, source_url FROM source_records "
                        "WHERE company_id = :cid AND provider_key = 'issuer_official' "
                        "ORDER BY source_url"
                    ),
                    {"cid": env["company_id"]},
                )
            )
            .mappings()
            .all()
        )
    urls = {r["source_url"] for r in rows}
    assert "https://catl.com/" in urls
    assert "https://catl.com/investor" in urls
    assert any(u.endswith(".pdf") for u in urls)  # 官网材料 PDF 落库
    assert all(r["document_type"] == "issuer_ir_material" for r in rows)
    assert all(r["provider_key"] == "issuer_official" for r in rows)
    assert all(r["published_at"] is None for r in rows)
    assert all(r["authority_tier_snapshot"] == 2 for r in rows)
    assert all(r["critical_claim_eligible_snapshot"] for r in rows)
    assert all(r["acquisition_method"] == "automatic_discovery" for r in rows)
    # 外域 / http / fragment-only 链接不被抓取。
    assert not any("other-domain" in u for u in html.calls)
    assert not any(u.startswith("http://") for u in html.calls)


async def test_discover_idempotent_skips_existing_url(env) -> None:
    """同一 (provider, source_url) 已落库 → 不重复抓取（跨 run 幂等）。"""
    pages = {
        "https://catl.com/": ("https://catl.com/", _HOME),
        "https://catl.com/investor": ("https://catl.com/investor", _IR_PAGE),
    }
    html = _FakeHtmlFetcher(dict(pages))
    provider = IrDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        html_fetcher=html,
        pdf_fetcher=_FakePdfFetcher(),
    )
    first = await provider.discover(_request(env))
    assert first.acquired
    calls_after_first = len(html.calls)

    # 第二次运行：全部 URL 已存在 → 不抓取，exhausted（诚实：无新来源）。
    html2 = _FakeHtmlFetcher(dict(pages))
    provider2 = IrDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        html_fetcher=html2,
        pdf_fetcher=_FakePdfFetcher(),
    )
    second = await provider2.discover(_request(env))
    assert not second.acquired
    assert second.exhausted
    assert html2.calls == [], f"不应重复抓取：{html2.calls}"
    assert calls_after_first >= 1


async def test_discover_disabled_exhausted(env) -> None:
    provider = IrDiscoveryProvider(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        html_fetcher=_FakeHtmlFetcher({}),
        enabled=False,
    )
    result = await provider.discover(_request(env))
    assert not result.acquired
    assert result.exhausted
