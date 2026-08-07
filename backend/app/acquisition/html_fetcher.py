"""Safe HTML fetcher for original news publishers (stage 2D.2A).

在 Resolver 已确认"URL 属于登记 Original Publisher"之后，才允许对本 URL
发起获取（Invariant B）。安全规则：
- 仅 https；hostname 必须属于同一 Publisher 的 allowed_domains；
- 请求前 DNS 预检（HostResolver）：任一解析 IP 为非全局地址即拒绝（SSRF 纵深防御）；
- trust_env=False；无 Cookie / Authorization / API Key / 浏览器 Headers / JS；
- 手动重定向 ≤5 次，每跳 urljoin 后重新做完整校验（跨 publisher / 降级 http 拒绝）；
- 仅接受 2xx；Content-Type 基础媒体类型必须 text/html（不接受 xhtml+xml）；
- 5 MiB 最大正文（Content-Length 提前拒绝 + 实际流式超限拒绝）；
- 保留原始字节（不强制 UTF-8、不重编码、不解析）。

错误映射：传输层/DNS/重定向/非 2xx → NewsOriginalFetchFailed；
Content-Type 非 text/html / 超限 / 空正文 → NewsOriginalContentRejected。
日志只记录 provider_key / hostname / status / duration_ms / error_type。
"""

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit

import httpx

from app.acquisition.host_resolver import (
    HostResolutionError,
    HostResolver,
    SystemHostResolver,
    validate_hostname_addresses,
)
from app.core.logging import get_logger
from app.news.errors import NewsOriginalContentRejected, NewsOriginalFetchFailed
from app.source_registry.url_policy import is_url_allowed

logger = get_logger("app.acquisition.html_fetcher")

_MAX_REDIRECTS = 5
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_BODY_BYTES = 5 * 1024 * 1024
_CHUNK_SIZE = 1024 * 1024
_HTML_MEDIA_TYPE = "text/html"

_DEFAULT_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)


@dataclass(frozen=True)
class FetchedHtmlPage:
    requested_url: str
    final_url: str
    final_hostname: str
    status_code: int
    content_type: str
    redirect_count: int
    fetched_at: datetime
    raw_bytes: bytes


class SafeHtmlFetcher:
    """安全获取单个原创发布者 HTML 页面。"""

    def __init__(
        self,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        resolver: HostResolver | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = timeout or _DEFAULT_TIMEOUT
        self._resolver = resolver if resolver is not None else SystemHostResolver()

    async def fetch(
        self,
        url: str,
        provider_key: str,
        allowed_domains: list[str],
    ) -> FetchedHtmlPage:
        requested_url = url
        current = url
        redirects = 0
        while True:
            started = time.monotonic()
            hostname = self._validate_url(current, allowed_domains)
            try:
                await self._preflight_dns(current)
            except NewsOriginalFetchFailed:
                logger.warning(
                    "html_fetch_dns_rejected",
                    provider_key=provider_key,
                    hostname=hostname,
                    error_type="host_resolution",
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            try:
                async with self._new_client() as client:
                    async with client.stream(
                        "GET",
                        current,
                        follow_redirects=False,
                    ) as response:
                        status = response.status_code
                        if status in _REDIRECT_CODES:
                            if redirects >= _MAX_REDIRECTS:
                                raise NewsOriginalFetchFailed()
                            location = response.headers.get("location")
                            if not location:
                                raise NewsOriginalFetchFailed()
                            next_url = urljoin(str(response.url), location)
                            self._validate_url(next_url, allowed_domains)
                            if next_url == current:
                                raise NewsOriginalFetchFailed()
                            current = next_url
                            redirects += 1
                            continue
                        if not 200 <= status < 300:
                            raise NewsOriginalFetchFailed()
                        page = await self._download(response, requested_url, current, redirects)
                        return page
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                logger.warning(
                    "html_fetch_failed",
                    provider_key=provider_key,
                    hostname=hostname,
                    error_type=type(exc).__name__,
                    duration_ms=int((time.monotonic() - started) * 1000),
                )
                raise NewsOriginalFetchFailed() from exc

    async def _preflight_dns(self, url: str) -> None:
        host = urlsplit(url).hostname
        if host is None:
            raise NewsOriginalFetchFailed()
        try:
            ips = await self._resolver.resolve(host)
            await validate_hostname_addresses(host, ips)
        except HostResolutionError as exc:
            raise NewsOriginalFetchFailed() from exc

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    @staticmethod
    def _validate_url(url: str, allowed_domains: list[str]) -> str:
        try:
            parsed = urlsplit(url)
        except ValueError:
            raise NewsOriginalFetchFailed() from None
        if parsed.scheme != "https":
            raise NewsOriginalFetchFailed()
        if parsed.fragment:
            raise NewsOriginalFetchFailed()
        if parsed.username is not None or parsed.password is not None:
            raise NewsOriginalFetchFailed()
        try:
            port = parsed.port
        except ValueError:
            # urlparse 对畸形 netloc（如无括号 IPv6 拼 host:port）抛异常
            raise NewsOriginalFetchFailed() from None
        if port is not None:
            raise NewsOriginalFetchFailed()
        host = parsed.hostname
        if not host:
            raise NewsOriginalFetchFailed()
        if not is_url_allowed(url, allowed_domains):
            raise NewsOriginalFetchFailed()
        return host

    @staticmethod
    async def _download(
        response: httpx.Response,
        requested_url: str,
        final_url: str,
        redirect_count: int,
    ) -> FetchedHtmlPage:
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        if content_type != _HTML_MEDIA_TYPE:
            raise NewsOriginalContentRejected()
        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                if int(content_length) > _MAX_BODY_BYTES:
                    raise NewsOriginalContentRejected()
            except ValueError:
                pass
        body = bytearray()
        size = 0
        async for chunk in response.aiter_bytes(chunk_size=_CHUNK_SIZE):
            size += len(chunk)
            if size > _MAX_BODY_BYTES:
                raise NewsOriginalContentRejected()
            body.extend(chunk)
        if not body:
            raise NewsOriginalContentRejected()
        final_hostname = urlsplit(final_url).hostname or ""
        return FetchedHtmlPage(
            requested_url=requested_url,
            final_url=final_url,
            final_hostname=final_hostname,
            status_code=response.status_code,
            content_type=content_type,
            redirect_count=redirect_count,
            fetched_at=datetime.now(UTC),
            raw_bytes=bytes(body),
        )
