"""Probe CLI helpers and _probe_provider unit tests (MockTransport only)."""

from dataclasses import replace
from datetime import date, datetime

import httpx
import pytest

from app.cli.probe_disclosure_sources import (
    _main,
    _matching_candidate_urls,
    _parse_date,
    _probe_provider,
    _resolve_allowed_url,
    probe_api_signals,
)
from app.disclosures.contracts import DisclosureProbeContext
from app.disclosures.decision import decide_access_mode, is_auto_discoverable
from app.disclosures.probe import (
    NOTE_NO_MATCHING_CANDIDATE_ROW,
    NOTE_OFFICIAL_PDF_LINK_VERIFIED,
    NOTE_PROBE_HTTP_ERROR,
    NOTE_SEARCH_REQUEST_NOT_APPLIED,
    DisclosureAccessMode,
    DisclosureProbeResult,
)
from app.disclosures.probe_client import Link, ProbeClient


def _context(security_code: str = "600519") -> DisclosureProbeContext:
    return DisclosureProbeContext(
        security_code=security_code,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 8, 7),
    )


def test_parse_date_ok() -> None:
    assert _parse_date("2026-01-01") == date(2026, 1, 1)


def test_parse_date_invalid_raises() -> None:
    with pytest.raises(ValueError):
        _parse_date("2026/01/01")


def test_resolve_allowed_url_relative_and_https() -> None:
    base = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    assert (
        _resolve_allowed_url("c/600519.pdf", base, ["www.sse.com.cn"])
        == "https://www.sse.com.cn/disclosure/listedinfo/announcement/c/600519.pdf"
    )


def test_resolve_allowed_url_rejects_http_and_non_allowlist() -> None:
    base = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    assert _resolve_allowed_url("http://www.sse.com.cn/x.pdf", base, ["www.sse.com.cn"]) is None
    assert _resolve_allowed_url("https://evil.example.com/x.pdf", base, ["www.sse.com.cn"]) is None


def test_matching_candidate_urls_filters_by_five_conditions() -> None:
    base = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    links = [
        Link(
            text="查看",
            href="c/2026-04-30/600519.pdf",
            base_url=base,
            context="600519 贵州茅台 2026-04-30 查看",
        ),
        Link(
            text="查看",
            href="c/2026-04-30/000001.pdf",
            base_url=base,
            context="000001 万科 2026-04-30 查看",
        ),
        Link(
            text="查看",
            href="c/2026-04-30/600519.pdf",
            base_url=base,
            context="600519 贵州茅台 查看",
        ),
        Link(
            text="",
            href="c/2026-04-30/600519.pdf",
            base_url=base,
            context="600519 2026-04-30",
        ),
        Link(
            text="查看",
            href="http://www.sse.com.cn/x.pdf",
            base_url=base,
            context="600519 2026-04-30 查看",
        ),
    ]
    urls = _matching_candidate_urls(links, "600519", ["www.sse.com.cn"])
    assert urls == [
        "https://www.sse.com.cn/disclosure/listedinfo/announcement/c/2026-04-30/600519.pdf"
    ]


def test_probe_api_signals_empty_body() -> None:
    assert probe_api_signals(b"", "https://www.sse.com.cn/") == (False, False)


def test_probe_api_signals_documented_api_in_link() -> None:
    body = '<a href="https://www.sse.com.cn/openapi-docs">开放平台文档</a>'.encode()
    assert probe_api_signals(body, "https://www.sse.com.cn/") == (True, False)


def test_probe_api_signals_generic_login_is_not_auth() -> None:
    # 仅出现"登录"不是认证信号，且无 API 文档入口。
    body = "<html>请先登录后查看</html>".encode()
    assert probe_api_signals(body, "https://www.sse.com.cn/") == (False, False)


def test_probe_api_signals_marker_outside_link_ignored() -> None:
    # API 文档标记不在锚点/href 中，不视为已确认入口。
    body = "<p>开放平台</p><p>swagger</p>".encode()
    assert probe_api_signals(body, "https://www.sse.com.cn/") == (False, False)


def test_probe_api_signals_api_plus_auth_terms() -> None:
    body = (
        '<a href="https://www.sse.com.cn/api-docs">API 文档</a><p>需要 api key 申请权限</p>'
    ).encode()
    assert probe_api_signals(body, "https://www.sse.com.cn/") == (True, True)


@pytest.mark.asyncio
async def test_probe_provider_reports_unavailable_on_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("mock timeout")

    client = ProbeClient(
        provider_key="sse",
        allowed_domains=["www.sse.com.cn"],
        transport=httpx.MockTransport(handler),
    )
    result = await _probe_provider(client, _context())
    assert result.access_mode == DisclosureAccessMode.UNAVAILABLE
    assert result.listing_page_reachable is False
    assert result.matching_candidate_count == 0
    assert result.request_count == 1
    assert NOTE_PROBE_HTTP_ERROR in result.notes


@pytest.mark.asyncio
async def test_probe_provider_homepage_hit_without_applied_search_is_direct_pdf_only() -> None:
    # 测试 A：初始首页偶然包含候选行且直接验证到官方 PDF，但查询未应用
    # （真实 CLI 没有合规查询入口）——只能判 public_direct_pdf_only，不选中。
    html = (
        "<table><tr><td>600519</td><td>贵州茅台：2025 年年度报告</td><td>2026-04-30</td>"
        '<td><a href="https://www.sse.com.cn/2026/600519.pdf">查看</a></td>'
        "</tr></table>"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pdf"):
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf", "content-length": "2048"},
                content=b"%PDF-1.7\n%%EOF\n",
            )
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    client = ProbeClient(
        provider_key="sse",
        allowed_domains=["www.sse.com.cn"],
        transport=httpx.MockTransport(handler),
    )
    result = await _probe_provider(client, _context())
    assert result.listing_page_reachable is True
    assert result.listing_status_code == 200
    assert result.final_hostname == "www.sse.com.cn"
    assert result.response_type == "html"
    assert result.matching_candidate_count == 1
    assert result.direct_pdf_verified is True
    assert result.search_request_applied is False
    assert result.access_mode == DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    assert is_auto_discoverable(result.access_mode) is False
    assert result.request_count == 2
    assert NOTE_OFFICIAL_PDF_LINK_VERIFIED in result.notes
    assert NOTE_SEARCH_REQUEST_NOT_APPLIED in result.notes


@pytest.mark.asyncio
async def test_server_rendered_html_requires_applied_search() -> None:
    # 测试 B：显式构造 search_request_applied=True 才允许 public_server_rendered_html；
    # 同样的证据若查询未应用则只能得到 public_direct_pdf_only。
    base = DisclosureProbeResult(
        provider_key="sse",
        checked_at=datetime(2026, 8, 7, 9, 0, 0),
        access_mode=DisclosureAccessMode.UNAVAILABLE,
        listing_page_reachable=True,
        listing_results_visible_in_html=True,
        direct_pdf_verified=True,
        documented_api_found=False,
        authentication_required=False,
        request_count=2,
        notes=(NOTE_OFFICIAL_PDF_LINK_VERIFIED,),
        listing_status_code=200,
        final_hostname="www.sse.com.cn",
        response_type="html",
        search_request_applied=True,
        matching_candidate_count=1,
    )
    applied = base
    not_applied = replace(base, search_request_applied=False)
    assert decide_access_mode(applied) == DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
    assert decide_access_mode(not_applied) == DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY


@pytest.mark.asyncio
async def test_probe_provider_shell_yields_discovery_not_confirmed() -> None:
    html = "<html><body><div>公告查询</div></body></html>".encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    client = ProbeClient(
        provider_key="sse",
        allowed_domains=["www.sse.com.cn"],
        transport=httpx.MockTransport(handler),
    )
    result = await _probe_provider(client, _context())
    assert result.listing_page_reachable is True
    assert result.matching_candidate_count == 0
    assert result.direct_pdf_verified is False
    assert result.search_request_applied is False
    assert result.access_mode == DisclosureAccessMode.DISCOVERY_NOT_CONFIRMED
    assert result.request_count == 1
    assert NOTE_NO_MATCHING_CANDIDATE_ROW in result.notes


@pytest.mark.asyncio
async def test_probe_provider_candidate_but_no_direct_pdf_is_not_confirmed() -> None:
    # 有候选行但文件链接不是可直接验证的 PDF：不推断需要 JS/内部接口，
    # 保守判为 discovery_not_confirmed。
    html = (
        "<table><tr><td>600519</td><td>贵州茅台：2025 年年度报告</td><td>2026-04-30</td>"
        '<td><a href="https://www.sse.com.cn/detail/600519/2026-04-30">详情</a></td>'
        "</tr></table>"
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    client = ProbeClient(
        provider_key="sse",
        allowed_domains=["www.sse.com.cn"],
        transport=httpx.MockTransport(handler),
    )
    result = await _probe_provider(client, _context())
    assert result.matching_candidate_count == 1
    assert result.direct_pdf_verified is False
    assert result.access_mode == DisclosureAccessMode.DISCOVERY_NOT_CONFIRMED


@pytest.mark.asyncio
async def test_main_rejects_non_allowlisted_provider() -> None:
    exit_code = await _main(["--providers", "szse", "--security-code", "000001"])
    assert exit_code == 2


@pytest.mark.asyncio
async def test_main_rejects_empty_provider_list() -> None:
    exit_code = await _main(["--providers", " , ", "--security-code", "000001"])
    assert exit_code == 2
