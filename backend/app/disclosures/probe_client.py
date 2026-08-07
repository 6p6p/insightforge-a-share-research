"""Controlled probe client for official disclosure sources.

只做少量、受控、可审计的真实探测：
- 仅允许 Source Registry 已登记且 enabled 的 Provider；
- 仅 https，URL 必须通过 allowed_domains；
- 不使用 Cookie、Authorization、自定义 Header，不自动重试；
- 不执行 JavaScript，不使用浏览器；
- 同域重定向仍重新执行 allowlist，跨域重定向拒绝；
- 单次 HTML 响应上限 2 MiB；PDF 探测使用流式 GET 只读取前 8192 字节
  文件头验证（Content-Type/签名/声明大小），不下载正文；
- 单个 Provider 最多 6 个请求；
- 日志只记录 provider_key、hostname、status、duration、response_type，
  不记录完整 query 与响应正文。
"""

import time
from dataclasses import dataclass, replace
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


class ProbePdfTooLarge(Exception):
    """PDF 声明大小（Content-Length）超过探测上限，未读取正文即拒绝。"""

    def __init__(self, declared_bytes: int) -> None:
        super().__init__(f"pdf declared size {declared_bytes} exceeds probe limit")
        self.declared_bytes = declared_bytes


class ProbeInvalidPdf(Exception):
    """PDF 探测失败：Content-Type 或签名不符。

    reason 取值："http_status" / "content_type" / "signature"。
    """

    def __init__(self, reason: str) -> None:
        super().__init__(f"invalid pdf probe result: {reason}")
        self.reason = reason


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

    async def probe_pdf(self, url: str) -> ProbeResponse:
        """流式 PDF 探测：只读取前 8192 字节验证，不下载正文。

        - 重定向重新执行 allowlist 校验，最多 5 次；
        - 只允许 2xx；Content-Type 去除参数后必须为 application/pdf；
        - Content-Length 声明超过 PDF_MAX_BYTES 立即拒绝；
        - 跳过开头空白后必须以 %PDF- 开头；验证完成立即关闭流；
        - 成功返回的 ProbeResponse.body 恒为空（正文不进入结果）。
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
            outcome: ProbeResponse | None = None
            redirect_to: str | None = None
            try:
                async with self._new_client() as client:
                    # 必须用流式 GET：client.get() 默认 stream=False 会在返回前
                    # 缓冲/读取整个响应正文（PDF 探测会实际下载完整 PDF）。
                    async with client.stream(
                        "GET",
                        current,
                        follow_redirects=False,
                        headers={"accept": accept},
                    ) as response:
                        status = response.status_code
                        final_url = str(response.url)
                        content_type = response.headers.get("content-type")
                        if status in _REDIRECT_CODES:
                            if redirects >= _MAX_REDIRECTS:
                                raise ProbeRedirectLoop(self._provider_key)
                            location = response.headers.get("location")
                            if not location:
                                raise ProbeRedirectLoop(self._provider_key)
                            next_url = str(httpx.URL(current).join(location))
                            # 跨域重定向：即使 http -> https 也拒绝（只允许 allowed_domains 内）
                            self._validate_url(next_url)
                            if next_url == current:
                                raise ProbeRedirectLoop(self._provider_key)
                            redirect_to = next_url
                        elif response_type == "pdf":
                            outcome = await self._probe_pdf(response, status, final_url, redirects)
                        elif 400 <= status < 600:
                            outcome = ProbeResponse(
                                status_code=status,
                                content_type=content_type,
                                body=b"",
                                final_url=final_url,
                                duration_ms=0,
                                response_type=response_type,
                                redirects=redirects,
                            )
                        else:
                            body = await self._read_limited(response, max_bytes)
                            outcome = ProbeResponse(
                                status_code=status,
                                content_type=content_type,
                                body=body,
                                final_url=final_url,
                                duration_ms=0,
                                response_type=response_type,
                                redirects=redirects,
                            )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                raise ProbeFetchError(self._provider_key, str(exc)) from exc

            duration_ms = int((time.monotonic() - started) * 1000)
            if redirect_to is not None:
                current = redirect_to
                redirects += 1
                self._log("probe_redirect", current, status, duration_ms)
                continue
            if outcome is None:
                raise ProbeFetchError(self._provider_key, "unexpected probe outcome")
            event = "probe_pdf" if response_type == "pdf" else "probe_ok"
            self._log(
                event,
                outcome.final_url,
                outcome.status_code,
                duration_ms,
                outcome.content_type,
            )
            return replace(outcome, duration_ms=duration_ms)

    async def _probe_pdf(
        self,
        response: httpx.Response,
        status: int,
        final_url: str,
        redirects: int,
    ) -> ProbeResponse:
        """PDF 验证不变量：任一条件不满足即抛异常，调用方无需再比对状态码。"""
        if not 200 <= status < 300:
            raise ProbeInvalidPdf("http_status")
        content_type = response.headers.get("content-type")
        if (
            content_type is None
            or content_type.split(";", 1)[0].strip().lower() != "application/pdf"
        ):
            raise ProbeInvalidPdf("content_type")
        content_length = response.headers.get("content-length")
        length: int | None = None
        if content_length and content_length.isdigit():
            length = int(content_length)
            if length > PDF_MAX_BYTES:
                raise ProbePdfTooLarge(length)
        prefix = await self._read_pdf_prefix(response)
        if not prefix.lstrip(b" \t\r\n\x00").startswith(b"%PDF-"):
            raise ProbeInvalidPdf("signature")
        return ProbeResponse(
            status_code=status,
            content_type=content_type,
            body=b"",
            final_url=final_url,
            duration_ms=0,
            response_type="pdf",
            redirects=redirects,
            length=length,
        )

    @staticmethod
    async def _read_pdf_prefix(response: httpx.Response) -> bytes:
        """流式读取最多 8192 字节文件头；不消费正文。"""
        prefix = b""
        async for chunk in response.aiter_bytes(chunk_size=8192):
            prefix += chunk
            if len(prefix) >= 8192:
                break
        return prefix[:8192]

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
