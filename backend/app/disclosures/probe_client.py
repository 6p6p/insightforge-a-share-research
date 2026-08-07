"""Controlled probe client for official disclosure sources.

只做少量、受控、可审计的真实探测：
- 仅允许 Source Registry 已登记且 enabled 的 Provider；
- 仅 https，URL 必须通过 allowed_domains；
- 不使用 Cookie、Authorization、自定义 Header，不自动重试；
- 不执行 JavaScript，不使用浏览器；
- 同域重定向仍重新执行 allowlist，跨域重定向拒绝；
- 单次 HTML 响应上限 2 MiB、PDF 探测上限 10 MiB；
- 单个 Provider 最多 6 个请求；
- 日志只记录 provider_key、hostname、status、duration、response_type，
  不记录完整 query 与响应正文。
"""

import time
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlparse

import httpx

from app.core.logging import get_logger
from app.source_registry.url_policy import is_url_allowed

HTML_MAX_BYTES = 2 * 1024 * 1024  # 2 MiB
PDF_MAX_BYTES = 10 * 1024 * 1024  # 10 MiB
PROVIDER_REQUEST_LIMIT = 6
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_REDIRECTS = 5

_TIMEOUT = httpx.Timeout(connect=10.0, read=20.0, write=10.0, pool=10.0)


@dataclass(frozen=True)
class ProbeResponse:
    status_code: int
    content_type: str | None
    body: bytes
    final_url: str
    duration_ms: int
    response_type: str  # "html" | "pdf" | "other"
    redirects: int
    length: int | None = None  # PDF 探测的 Content-Length；HTML 探测为 None


@dataclass(frozen=True)
class Link:
    """页面中提取出的一个 <a href> 链接及其上下文文本。

    - text：锚点文本（HTML entity 已解码、空白已归一化）；
    - href：原始 href 属性值（可能是相对 URL）；
    - base_url：页面最终 URL，供 urljoin 解析相对链接；
    - context：链接前到上一个链接之间的文本 + 锚点文本，供公司/日期匹配。
    """

    text: str
    href: str
    base_url: str
    context: str


class LinkExtractor(HTMLParser):
    """轻量 HTML 链接提取器：只处理 <a href>，不执行 JS、不解析 onclick。

    - 忽略 script/style 内的文本与链接；
    - 忽略 javascript:、空 href、纯 # 锚点；
    - 相对 URL 原样保留，由调用方 urljoin 后重新执行 allowlist 校验。
    """

    def __init__(self, base_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self._base_url = base_url
        self.links: list[Link] = []
        self._href: str | None = None
        self._anchor: list[str] = []
        self._buffer: list[str] = []
        self._script_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in ("script", "style"):
            self._script_depth += 1
            return
        if tag == "a" and self._script_depth == 0:
            href = dict(attrs).get("href")
            if href is not None and self._usable_href(href):
                self._href = href
                self._anchor = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            if self._script_depth > 0:
                self._script_depth -= 1
            return
        if tag == "a" and self._href is not None and self._script_depth == 0:
            anchor = self._clean("".join(self._anchor))
            context = self._clean("".join(self._buffer) + "".join(self._anchor))
            self.links.append(
                Link(
                    text=anchor,
                    href=self._href,
                    base_url=self._base_url,
                    context=context,
                )
            )
            self._buffer = [anchor] if anchor else []
            self._href = None
            self._anchor = []

    def handle_data(self, data: str) -> None:
        if self._script_depth > 0:
            return
        if self._href is not None:
            self._anchor.append(data)
        else:
            self._buffer.append(data)

    @staticmethod
    def _usable_href(href: str) -> bool:
        href = href.strip()
        if not href:
            return False
        if href.startswith("javascript:"):
            return False
        if href.startswith("#"):
            return False
        return True

    @staticmethod
    def _clean(value: str) -> str:
        return " ".join(value.split())


def extract_links(body: bytes, base_url: str) -> list[Link]:
    """从响应正文中提取全部合规候选 <a href> 链接。"""
    parser = LinkExtractor(base_url)
    parser.feed(body.decode("utf-8", errors="replace"))
    return parser.links


class ProbeLimitExceeded(Exception):
    """单次探测请求次数超限。"""


class ProbeUrlNotAllowed(Exception):
    """探测目标不在 Provider allowed_domains 内。"""


class ProbeResponseTooLarge(Exception):
    """响应正文超过探测上限。"""


class ProbeClient:
    """受控探测客户端；同一个实例用于一个 Provider 的全部探测。"""

    def __init__(
        self,
        *,
        provider_key: str,
        allowed_domains: list[str],
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        provider_request_limit: int = PROVIDER_REQUEST_LIMIT,
    ) -> None:
        self._provider_key = provider_key
        self._allowed_domains = list(allowed_domains)
        self._transport = transport
        self._timeout = timeout or _TIMEOUT
        self._provider_request_limit = provider_request_limit
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    @property
    def allowed_domains(self) -> list[str]:
        return list(self._allowed_domains)

    def _next_request(self) -> None:
        if self._request_count >= self._provider_request_limit:
            raise ProbeLimitExceeded(
                f"provider {self._provider_key} 探测请求数超过上限 {self._provider_request_limit}"
            )
        self._request_count += 1

    def _validate_url(self, url: str) -> None:
        try:
            parsed = urlparse(url)
        except ValueError:
            raise ProbeUrlNotAllowed(url) from None
        if parsed.scheme != "https":
            raise ProbeUrlNotAllowed(url)
        if parsed.username is not None or parsed.password is not None:
            raise ProbeUrlNotAllowed(url)
        if parsed.port is not None:
            raise ProbeUrlNotAllowed(url)
        if not is_url_allowed(url, self._allowed_domains):
            raise ProbeUrlNotAllowed(url)

    async def fetch_page(
        self,
        url: str,
        max_bytes: int = HTML_MAX_BYTES,
        response_type: str = "html",
    ) -> ProbeResponse:
        """GET 一个页面（HTML 或 PDF 探测）；不跟随跨域重定向。"""
        return await self._fetch(
            url,
            max_bytes=max_bytes,
            response_type=response_type,
            accept="*/*",
        )

    async def fetch_pdf_head(self, url: str) -> ProbeResponse:
        """PDF 探测：不拉取正文，仅确认可达性与 Content-Type。

        重定向重新执行 allowlist 校验；Content-Type 以响应头为准，
        由调用方比对 application/pdf。
        """
        return await self._fetch(
            url,
            max_bytes=0,
            response_type="pdf",
            accept="application/pdf",
        )

    async def _fetch(
        self,
        url: str,
        *,
        max_bytes: int,
        response_type: str,
        accept: str,
    ) -> ProbeResponse:
        self._validate_url(url)
        current = url
        redirects = 0
        while True:
            self._next_request()
            started = time.monotonic()
            async with self._new_client() as client:
                try:
                    response = await client.get(
                        current,
                        follow_redirects=False,
                        headers={"accept": accept},
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise ProbeFetchError(self._provider_key, str(exc)) from exc
            duration_ms = int((time.monotonic() - started) * 1000)

            if response.status_code in _REDIRECT_CODES:
                if redirects >= _MAX_REDIRECTS:
                    raise ProbeRedirectLoop(self._provider_key)
                location = response.headers.get("location")
                if not location:
                    raise ProbeRedirectLoop(self._provider_key)
                next_url = str(httpx.URL(current).join(location))
                # 跨域重定向：即使 http -> https 也拒绝（探测只允许 allowed_domains 内）
                self._validate_url(next_url)
                if next_url == current:
                    raise ProbeRedirectLoop(self._provider_key)
                current = next_url
                redirects += 1
                self._log("probe_redirect", current, response.status_code, duration_ms)
                continue

            if 400 <= response.status_code < 600:
                await response.aclose()
                return ProbeResponse(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    body=b"",
                    final_url=str(response.url),
                    duration_ms=duration_ms,
                    response_type=response_type,
                    redirects=redirects,
                )

            if response_type == "pdf":
                # PDF 探测只确认可达性与 Content-Type，不读取正文。
                content_length = response.headers.get("content-length")
                length = (
                    int(content_length) if content_length and content_length.isdigit() else None
                )
                self._log(
                    "probe_pdf",
                    current,
                    response.status_code,
                    duration_ms,
                    response.headers.get("content-type"),
                )
                await response.aclose()
                return ProbeResponse(
                    status_code=response.status_code,
                    content_type=response.headers.get("content-type"),
                    body=b"",
                    final_url=str(response.url),
                    duration_ms=duration_ms,
                    response_type="pdf",
                    redirects=redirects,
                    length=length,
                )

            body = await self._read_limited(response, max_bytes)
            self._log(
                "probe_ok",
                current,
                response.status_code,
                duration_ms,
                response.headers.get("content-type"),
            )
            return ProbeResponse(
                status_code=response.status_code,
                content_type=response.headers.get("content-type"),
                body=body,
                final_url=str(response.url),
                duration_ms=duration_ms,
                response_type=response_type,
                redirects=redirects,
            )

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
    async def _read_limited(response: httpx.Response, max_bytes: int) -> bytes:
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes():
            total += len(chunk)
            if total > max_bytes:
                await response.aclose()
                raise ProbeResponseTooLarge()
            chunks.append(chunk)
        return b"".join(chunks)

    def _log(
        self,
        event: str,
        url: str,
        status: int,
        duration_ms: int,
        content_type: str | None = None,
    ) -> None:
        hostname = urlparse(url).hostname or ""
        response_type = "pdf" if content_type == "application/pdf" else "html"
        # 延迟解析 logger，让调用方（如 CLI）能先配置日志目标（stderr）再使用。
        get_logger("app.disclosures.probe").info(
            event,
            provider_key=self._provider_key,
            hostname=hostname,
            status=status,
            duration_ms=duration_ms,
            response_type=response_type,
        )


class ProbeFetchError(Exception):
    """探测请求本身失败（超时 / 传输错误），不暴露具体 URL 细节。"""

    def __init__(self, provider_key: str, reason: str) -> None:
        super().__init__(f"probe fetch failed for provider {provider_key}: {reason}")
        self.provider_key = provider_key


class ProbeRedirectLoop(Exception):
    """同域重定向超过限制。"""

    def __init__(self, provider_key: str) -> None:
        super().__init__(f"probe redirect loop for provider {provider_key}")
        self.provider_key = provider_key
