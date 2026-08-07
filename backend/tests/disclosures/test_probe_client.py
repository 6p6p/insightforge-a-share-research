"""ProbeClient controlled-network tests.

全部使用 httpx.MockTransport（禁真实外网）：验证官方 allowlist、大小上限、
同域重定向、跨域拒绝、错误响应、超时、请求限额、无 Cookie/Auth、无正文落盘。
"""

from urllib.parse import urljoin

import httpx
import pytest

from app.disclosures.probe_client import (
    ProbeClient,
    ProbeFetchError,
    ProbeLimitExceeded,
    ProbeRedirectLoop,
    ProbeResponseTooLarge,
    ProbeUrlNotAllowed,
    extract_links,
)


def _client(
    *,
    allowed_domains: list[str] | None = None,
    transport: httpx.MockTransport | None = None,
    provider_request_limit: int = 6,
) -> ProbeClient:
    return ProbeClient(
        provider_key="sse",
        allowed_domains=allowed_domains or ["www.sse.com.cn"],
        transport=transport or httpx.MockTransport(_ok_html),
        provider_request_limit=provider_request_limit,
    )


def _ok_html(request: httpx.Request) -> httpx.Response:
    content = "<html>公告披露</html>".encode()
    return httpx.Response(200, content=content, headers={"content-type": "text/html"})


@pytest.mark.asyncio
async def test_fetch_page_ok_within_allowlist() -> None:
    client = _client()
    response = await client.fetch_page("https://www.sse.com.cn/disclosure/announcement/")
    assert response.status_code == 200
    assert response.response_type == "html"
    assert "公告披露".encode() in response.body
    assert client.request_count == 1


@pytest.mark.asyncio
async def test_fetch_page_rejects_non_allowlist_with_zero_requests() -> None:
    client = _client()
    with pytest.raises(ProbeUrlNotAllowed):
        await client.fetch_page("https://evil.example.com/x.html")
    assert client.request_count == 0


@pytest.mark.asyncio
async def test_fetch_page_rejects_http_scheme() -> None:
    client = _client()
    with pytest.raises(ProbeUrlNotAllowed):
        await client.fetch_page("http://www.sse.com.cn/x.html")
    assert client.request_count == 0


@pytest.mark.asyncio
async def test_fetch_page_rejects_url_with_credentials_and_port() -> None:
    client = _client()
    with pytest.raises(ProbeUrlNotAllowed):
        await client.fetch_page("https://user:pass@www.sse.com.cn/x.html")
    with pytest.raises(ProbeUrlNotAllowed):
        await client.fetch_page("https://www.sse.com.cn:8443/x.html")


@pytest.mark.asyncio
async def test_fetch_page_too_large_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 1024, headers={"content-type": "text/html"})

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProbeResponseTooLarge):
        await client.fetch_page("https://www.sse.com.cn/x.html", max_bytes=100)


@pytest.mark.asyncio
async def test_same_domain_redirect_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old":
            return httpx.Response(302, headers={"location": "/new"})
        content = "<html>公告</html>".encode()
        return httpx.Response(200, content=content, headers={"content-type": "text/html"})

    client = _client(transport=httpx.MockTransport(handler))
    response = await client.fetch_page("https://www.sse.com.cn/old")
    assert response.status_code == 200
    assert response.final_url == "https://www.sse.com.cn/new"
    assert response.redirects == 1
    assert client.request_count == 2


@pytest.mark.asyncio
async def test_cross_domain_redirect_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/new"})

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProbeUrlNotAllowed):
        await client.fetch_page("https://www.sse.com.cn/old")
    assert client.request_count == 1


@pytest.mark.asyncio
async def test_redirect_without_location_is_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={})

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProbeRedirectLoop):
        await client.fetch_page("https://www.sse.com.cn/old")


@pytest.mark.asyncio
async def test_redirect_self_is_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/old"})

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProbeRedirectLoop):
        await client.fetch_page("https://www.sse.com.cn/old")


@pytest.mark.asyncio
async def test_client_error_returns_empty_body_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"content-type": "text/html"})

    client = _client(transport=httpx.MockTransport(handler))
    response = await client.fetch_page("https://www.sse.com.cn/x.html")
    assert response.status_code == 403
    assert response.body == b""


@pytest.mark.asyncio
async def test_transport_timeout_raises_probe_fetch_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("mock timeout")

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProbeFetchError):
        await client.fetch_page("https://www.sse.com.cn/x.html")


@pytest.mark.asyncio
async def test_request_limit_enforced() -> None:
    client = _client(provider_request_limit=2)
    await client.fetch_page("https://www.sse.com.cn/a")
    await client.fetch_page("https://www.sse.com.cn/b")
    with pytest.raises(ProbeLimitExceeded):
        await client.fetch_page("https://www.sse.com.cn/c")
    assert client.request_count == 2


@pytest.mark.asyncio
async def test_no_cookie_or_auth_headers_sent() -> None:
    seen: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        seen["accept"] = request.headers.get("accept")
        content = "<html>公告</html>".encode()
        return httpx.Response(200, content=content, headers={"content-type": "text/html"})

    client = _client(transport=httpx.MockTransport(handler))
    await client.fetch_page("https://www.sse.com.cn/x.html")
    assert seen["cookie"] is None
    assert seen["authorization"] is None
    assert seen["accept"] == "*/*"


@pytest.mark.asyncio
async def test_fetch_pdf_head_uses_head_semantics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("accept") == "application/pdf"
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf", "content-length": "2048"},
        )

    client = _client(transport=httpx.MockTransport(handler))
    response = await client.fetch_pdf_head("https://www.sse.com.cn/2026/000001.pdf")
    assert response.status_code == 200
    assert response.response_type == "pdf"
    assert response.length == 2048
    assert response.body == b""


@pytest.mark.asyncio
async def test_fetch_pdf_head_same_domain_redirect_revalidated() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/old.pdf":
            return httpx.Response(302, headers={"location": "/new.pdf"})
        return httpx.Response(
            200,
            headers={"content-type": "application/pdf"},
        )

    client = _client(transport=httpx.MockTransport(handler))
    response = await client.fetch_pdf_head("https://www.sse.com.cn/old.pdf")
    assert response.status_code == 200
    assert response.content_type == "application/pdf"
    assert response.final_url == "https://www.sse.com.cn/new.pdf"
    assert response.redirects == 1
    assert client.request_count == 2


@pytest.mark.asyncio
async def test_fetch_pdf_head_cross_domain_redirect_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/new.pdf"})

    client = _client(transport=httpx.MockTransport(handler))
    with pytest.raises(ProbeUrlNotAllowed):
        await client.fetch_pdf_head("https://www.sse.com.cn/old.pdf")
    assert client.request_count == 1


def test_extract_links_absolute_relative_and_query() -> None:
    base = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    body = (
        '<a href="https://www.sse.com.cn/2026/600519.pdf">绝对链接</a>'
        '<a href="c/2026-04-30/600519.pdf">相对链接</a>'
        '<a href="detail?id=123">详情页</a>'
    ).encode()
    links = extract_links(body, base)
    assert [urljoin(base, link.href) for link in links] == [
        "https://www.sse.com.cn/2026/600519.pdf",
        "https://www.sse.com.cn/disclosure/listedinfo/announcement/c/2026-04-30/600519.pdf",
        "https://www.sse.com.cn/disclosure/listedinfo/announcement/detail?id=123",
    ]


def test_extract_links_skips_javascript_fragment_and_empty() -> None:
    body = (
        b'<a href="javascript:void(0)">js</a>'
        b'<a href="#">frag</a>'
        b'<a href="">empty</a>'
        b'<a href="https://www.sse.com.cn/real.pdf">real</a>'
    )
    links = extract_links(body, "https://www.sse.com.cn/")
    assert len(links) == 1
    assert links[0].href == "https://www.sse.com.cn/real.pdf"


def test_extract_links_decodes_html_entities() -> None:
    body = '<a href="https://www.sse.com.cn/a.pdf">2025 &amp; 年度报告</a>'.encode()
    links = extract_links(body, "https://www.sse.com.cn/")
    assert links[0].text == "2025 & 年度报告"


def test_extract_links_ignores_script_and_style_text() -> None:
    body = (
        "<script>var x = '600519';</script>"
        "<style>.announcement { display: none; }</style>"
        "<a href='https://www.sse.com.cn/b.pdf'>下载</a>"
    ).encode()
    links = extract_links(body, "https://www.sse.com.cn/")
    assert len(links) == 1
    assert "600519" not in links[0].context
    assert links[0].text == "下载"


def test_link_context_builds_row_text() -> None:
    body = (
        "<tr><td>600519</td><td>贵州茅台：2025 年年度报告</td><td>2026-04-30</td>"
        '<td><a href="2026/600519.pdf">查看</a></td></tr>'
    ).encode()
    base = "https://www.sse.com.cn/disclosure/listedinfo/announcement/"
    links = extract_links(body, base)
    assert len(links) == 1
    link = links[0]
    assert "600519" in link.context
    assert "贵州茅台" in link.context
    assert "2026-04-30" in link.context
    assert link.href == "2026/600519.pdf"
