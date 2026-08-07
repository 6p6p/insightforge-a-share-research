"""Probe CLI helpers and _probe_provider unit tests (MockTransport only)."""

from datetime import date

import httpx
import pytest

from app.cli.probe_disclosure_sources import (
    _main,
    _parse_date,
    _probe_provider,
    first_pdf_link,
    looks_like_results_html,
    probe_api_signals,
)
from app.disclosures.probe import (
    NOTE_OFFICIAL_PDF_LINK_VERIFIED,
    NOTE_PROBE_HTTP_ERROR,
    DisclosureAccessMode,
)
from app.disclosures.probe_client import ProbeClient


def test_parse_date_ok() -> None:
    assert _parse_date("2026-01-01") == date(2026, 1, 1)


def test_parse_date_invalid_raises() -> None:
    with pytest.raises(ValueError):
        _parse_date("2026/01/01")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("<html>公告披露页面</html>", True),
        ("<html>董事会决议</html>", True),
        ("<html>empty shell</html>", False),
        ("", False),
    ],
)
def test_looks_like_results_html(text: str, expected: bool) -> None:
    assert looks_like_results_html(text) is expected


def test_first_pdf_link_finds_https_pdf() -> None:
    body = '<a href="https://www.sse.com.cn/2026/600519.pdf">下载</a>'.encode()
    assert first_pdf_link(body) == "https://www.sse.com.cn/2026/600519.pdf"


def test_first_pdf_link_finds_single_quoted() -> None:
    body = "<a href='https://www.sse.com.cn/2026/600519.pdf'>下载</a>".encode()
    assert first_pdf_link(body) == "https://www.sse.com.cn/2026/600519.pdf"


def test_first_pdf_link_ignores_http_and_non_pdf() -> None:
    body = (
        b'<a href="http://www.sse.com.cn/x.pdf">http</a>'
        b'<a href="https://www.sse.com.cn/y.html">html</a>'
    )
    assert first_pdf_link(body) is None


def test_first_pdf_link_none_when_no_link() -> None:
    assert first_pdf_link(b"<html>no links</html>") is None


def test_probe_api_signals_empty_body() -> None:
    assert probe_api_signals(b"") == (False, False)


def test_probe_api_signals_documented_api() -> None:
    assert probe_api_signals("<html>openapi 文档</html>".encode()) == (True, False)


def test_probe_api_signals_auth_marker() -> None:
    assert probe_api_signals("<html>请先登录</html>".encode()) == (False, True)


def test_probe_api_signals_both() -> None:
    assert probe_api_signals("<html>openapi 需要 api key 注册</html>".encode()) == (
        True,
        True,
    )


@pytest.mark.asyncio
async def test_probe_provider_reports_unavailable_on_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("mock timeout")

    client = ProbeClient(
        provider_key="sse",
        allowed_domains=["www.sse.com.cn"],
        transport=httpx.MockTransport(handler),
    )
    result = await _probe_provider(client, "600519")
    assert result.access_mode == DisclosureAccessMode.UNAVAILABLE
    assert result.listing_page_reachable is False
    assert result.request_count == 1
    assert NOTE_PROBE_HTTP_ERROR in result.notes


@pytest.mark.asyncio
async def test_probe_provider_success_yields_server_rendered_html() -> None:
    html = (
        "<html><body>公告披露 600519 "
        '<a href="https://www.sse.com.cn/2026/600519.pdf">下载</a></body></html>'
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".pdf"):
            return httpx.Response(
                200,
                headers={"content-type": "application/pdf", "content-length": "2048"},
            )
        return httpx.Response(200, content=html, headers={"content-type": "text/html"})

    client = ProbeClient(
        provider_key="sse",
        allowed_domains=["www.sse.com.cn"],
        transport=httpx.MockTransport(handler),
    )
    result = await _probe_provider(client, "600519")
    assert result.listing_page_reachable is True
    assert result.listing_results_visible_in_html is True
    assert result.direct_pdf_verified is True
    assert result.access_mode == DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
    assert result.request_count == 2
    assert NOTE_OFFICIAL_PDF_LINK_VERIFIED in result.notes


@pytest.mark.asyncio
async def test_main_rejects_non_allowlisted_provider() -> None:
    exit_code = await _main(["--providers", "szse", "--security-code", "000001"])
    assert exit_code == 2


@pytest.mark.asyncio
async def test_main_rejects_empty_provider_list() -> None:
    exit_code = await _main(["--providers", " , ", "--security-code", "000001"])
    assert exit_code == 2
