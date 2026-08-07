"""Tests for SafeHtmlFetcher (stage 2D.2A, §二十三).

全部 MockTransport + 假 DNS resolver，零真实网络。覆盖：
- happy path：2xx + text/html → 原始字节逐字节保留；
- Content-Type 非 text/html（含 xhtml+xml）→ NewsOriginalContentRejected；
- SSRF 纵深防御：私有 / 保留 / loopback / link-local IPv4/IPv6 → 拒绝；
- 全局 IPv4/IPv6 放行；
- 手动重定向 ≤5：同 publisher 跟随（redirect_count 记录）、跨 publisher /
  自循环 / 超 5 次拒绝；
- 5 MiB 上限：Content-Length 提前拒绝 + 实际流式超限拒绝；
- 空 DNS / 空正文 / 非 2xx 拒绝；
- URL 安全规则：http / fragment / userinfo / 端口 / 非 allowlist 域名拒绝；
- 无 Cookie / 无 Authorization / 非浏览器 UA（python-httpx 默认）；
- trust_env=False（spy AsyncClient 构造参数）；
- 失败日志不泄露正文（body-not-in-logs）。
"""

import hashlib

import httpx
import pytest

from app.acquisition.host_resolver import HostResolver
from app.acquisition.html_fetcher import SafeHtmlFetcher
from app.news.errors import NewsOriginalContentRejected, NewsOriginalFetchFailed

pytestmark = pytest.mark.asyncio

_HTML = b"<html><head><title>News</title></head><body>hello</body></html>"
_DOMAIN = "xinhuanet.com"
_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"
_MAX_BODY = 5 * 1024 * 1024


class FakeResolver(HostResolver):
    def __init__(self, ips: list[str] | None = None) -> None:
        self._ips = ips if ips is not None else ["93.184.216.34"]

    async def resolve(self, hostname: str) -> list[str]:
        return self._ips


def _page_router(
    content: bytes = _HTML,
    status: int = 200,
    content_type: str = "text/html",
    headers: dict | None = None,
) -> httpx.MockTransport:
    merged = {"content-type": content_type}
    if headers:
        merged.update(headers)
    return httpx.MockTransport(
        lambda request: httpx.Response(status, content=content, headers=merged)
    )


def _fetcher(
    router: httpx.AsyncBaseTransport,
    resolver: FakeResolver | None = None,
) -> SafeHtmlFetcher:
    return SafeHtmlFetcher(transport=router, resolver=resolver or FakeResolver())


# ---------------------------------------------------------------- happy path


async def test_fetches_html_page_and_preserves_raw_bytes() -> None:
    page = await _fetcher(_page_router()).fetch(_URL, "xinhuanet", [_DOMAIN])
    assert page.requested_url == _URL
    assert page.final_url == _URL
    assert page.final_hostname == "www.xinhuanet.com"
    assert page.status_code == 200
    assert page.content_type == "text/html"
    assert page.redirect_count == 0
    assert page.raw_bytes == _HTML
    assert hashlib.sha256(page.raw_bytes).hexdigest() == hashlib.sha256(_HTML).hexdigest()


async def test_content_type_with_charset_accepted() -> None:
    page = await _fetcher(_page_router(content_type="text/html; charset=utf-8")).fetch(
        _URL, "xinhuanet", [_DOMAIN]
    )
    assert page.content_type == "text/html"


# ---------------------------------------------------------------- content-type


async def test_rejects_non_html_content_type() -> None:
    with pytest.raises(NewsOriginalContentRejected):
        await _fetcher(_page_router(content_type="application/xhtml+xml")).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


async def test_rejects_json_content_type() -> None:
    with pytest.raises(NewsOriginalContentRejected):
        await _fetcher(_page_router(content=_HTML, content_type="application/json")).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


# ---------------------------------------------------------------- SSRF preflight


async def test_rejects_private_ipv4() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(), FakeResolver(["10.0.0.1"])).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


async def test_rejects_link_local_ipv4() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(), FakeResolver(["169.254.0.1"])).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


async def test_rejects_loopback_ipv4() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(), FakeResolver(["127.0.0.1"])).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


async def test_rejects_private_ipv6_link_local() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(), FakeResolver(["fe80::1"])).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


async def test_rejects_when_any_resolved_ip_non_global() -> None:
    # 任一解析 IP 非全局即拒绝（无法控制 OS 连接哪一个）
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(), FakeResolver(["93.184.216.34", "10.0.0.1"])).fetch(
            _URL, "xinhuanet", [_DOMAIN]
        )


async def test_accepts_global_ipv6() -> None:
    page = await _fetcher(
        _page_router(),
        FakeResolver(["2606:2800:220:1:248:1893:25c8:1946"]),
    ).fetch(_URL, "xinhuanet", [_DOMAIN])
    assert page.status_code == 200


async def test_rejects_empty_dns_resolution() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(), FakeResolver([])).fetch(_URL, "xinhuanet", [_DOMAIN])


# ---------------------------------------------------------------- redirects


def _redirect_router(target: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": target})
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    return httpx.MockTransport(handler)


async def test_follows_same_publisher_redirect() -> None:
    target = "https://www.xinhuanet.com/2026/0807/final.htm"
    page = await _fetcher(_redirect_router(target)).fetch(
        "https://www.xinhuanet.com/redirect", "xinhuanet", [_DOMAIN]
    )
    assert page.final_url == target
    assert page.final_hostname == "www.xinhuanet.com"
    assert page.redirect_count == 1
    assert page.raw_bytes == _HTML


async def test_rejects_cross_publisher_redirect() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_redirect_router("https://evil.example.org/x.htm")).fetch(
            "https://www.xinhuanet.com/redirect", "xinhuanet", [_DOMAIN]
        )


async def test_rejects_redirect_loop() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(httpx.MockTransport(handler)).fetch(_URL, "xinhuanet", [_DOMAIN])


async def test_rejects_more_than_five_redirects() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        n = int(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(
            302,
            headers={"location": f"https://www.xinhuanet.com/redirect/{n + 1}"},
        )

    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(httpx.MockTransport(handler)).fetch(
            "https://www.xinhuanet.com/redirect/0", "xinhuanet", [_DOMAIN]
        )


# ---------------------------------------------------------------- size limits


async def test_rejects_content_length_over_limit() -> None:
    router = _page_router(
        content=_HTML,
        headers={"content-length": str(_MAX_BODY + 1)},
    )
    with pytest.raises(NewsOriginalContentRejected):
        await _fetcher(router).fetch(_URL, "xinhuanet", [_DOMAIN])


async def test_rejects_stream_over_limit_without_content_length() -> None:
    oversized = b"x" * (_MAX_BODY + 1)
    with pytest.raises(NewsOriginalContentRejected):
        await _fetcher(_page_router(content=oversized)).fetch(_URL, "xinhuanet", [_DOMAIN])


async def test_rejects_empty_body() -> None:
    with pytest.raises(NewsOriginalContentRejected):
        await _fetcher(_page_router(content=b"")).fetch(_URL, "xinhuanet", [_DOMAIN])


# ---------------------------------------------------------------- HTTP status


async def test_rejects_non_2xx_status() -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router(status=500)).fetch(_URL, "xinhuanet", [_DOMAIN])


# ---------------------------------------------------------------- URL safety


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://www.xinhuanet.com/a.htm",
        "https://www.xinhuanet.com/a.htm#frag",
        "https://user@www.xinhuanet.com/a.htm",
        "https://www.xinhuanet.com:8443/a.htm",
        "https://evil.example.org/a.htm",
        "https://evilxinhuanet.com/a.htm",
    ],
)
async def test_rejects_unsafe_url(bad_url: str) -> None:
    with pytest.raises(NewsOriginalFetchFailed):
        await _fetcher(_page_router()).fetch(bad_url, "xinhuanet", [_DOMAIN])


# ---------------------------------------------------------------- 无浏览器特征


async def test_sends_no_cookie_or_authorization_headers() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["cookie"] = request.headers.get("cookie")
        seen["authorization"] = request.headers.get("authorization")
        seen["user_agent"] = request.headers.get("user-agent")
        return httpx.Response(200, content=_HTML, headers={"content-type": "text/html"})

    await _fetcher(httpx.MockTransport(handler)).fetch(_URL, "xinhuanet", [_DOMAIN])
    assert seen["cookie"] is None
    assert seen["authorization"] is None
    # 不伪装浏览器：UA 是 httpx 默认，而非 Chrome/Safari 等浏览器标识
    assert seen["user_agent"] is not None
    assert seen["user_agent"].startswith("python-httpx")
    assert "Mozilla" not in seen["user_agent"]


async def test_client_uses_trust_env_false(monkeypatch) -> None:
    captured: dict = {}
    original_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs) -> None:
        captured.update(kwargs)
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    page = await _fetcher(_page_router()).fetch(_URL, "xinhuanet", [_DOMAIN])
    assert page.status_code == 200
    assert captured["trust_env"] is False
    assert captured["follow_redirects"] is False


# ---------------------------------------------------------------- 日志不泄露正文


async def test_failure_logs_do_not_contain_body() -> None:
    """失败日志只记录 provider_key/hostname/error_type，不泄露异常内容/正文。"""
    from structlog.testing import capture_logs

    body_marker = "<html>SECRET-MARKER-xyz</html>"

    def handler(request: httpx.Request) -> httpx.Response:
        # 传输层失败：异常消息里夹带"正文"标记，验证日志只记录 error_type
        # 而非 str(exc)（防止未来把异常全文写进日志）。
        raise httpx.ConnectError(body_marker)

    with pytest.raises(NewsOriginalFetchFailed):
        with capture_logs() as captured:
            await _fetcher(httpx.MockTransport(handler)).fetch(_URL, "xinhuanet", [_DOMAIN])

    assert captured, "expected at least one warning record"
    for record in captured:
        assert body_marker not in record.get("event", "")
        assert record["provider_key"] == "xinhuanet"
        assert record["hostname"] == "www.xinhuanet.com"
        assert record["error_type"] == "ConnectError"
