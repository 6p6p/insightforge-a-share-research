"""Secure HTTP client for the GDELT DOC 2.0 API (stage 2D.1).

安全规则（对照规范十节）：
- 仅 https；host 固定 api.gdeltproject.org；endpoint 固定 /api/v2/doc/doc；
- 只允许 mode=artlist&format=json&sort=datedesc&maxrecords&startdatetime&enddatetime，
  不接受任意 endpoint / 额外 query 参数；query 表达式来自 NewsDiscoveryQuery.query_text；
- trust_env=False；不发送 Cookie / Authorization / API Key；不接受用户 Header；
- follow_redirects=False；手动重定向最多 3 次，redirect 后 hostname 必须仍为 API_HOST，
  跨 host 拒绝；不自动重试；
- 429 / 5xx / 其他非 2xx → 稳定错误（GdeltRequestFailed）；
- 单响应正文上限 5 MiB（流式读取 + 大小限制）；Content-Type 必须为 application/json；
- JSON 解析使用 parse_float=Decimal 并显式拒绝 NaN / Infinity / -Infinity；
- 日志只记录 engine、hostname、status、duration_ms、error_type；
  result_count 由 Provider 在解析后单独记录；不记录完整 URL query / query_text / 响应正文。
"""

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from app.core.logging import get_logger
from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryQuery
from app.news.gdelt.errors import (
    GdeltInvalidContentType,
    GdeltInvalidJson,
    GdeltRequestFailed,
    GdeltResponseTooLarge,
)
from app.news.provider import NewsRawDiscoveryResponse

API_HOST = "api.gdeltproject.org"
API_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
_MAX_REDIRECTS = 3
_REDIRECT_CODES = (301, 302, 303, 307, 308)

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def _build_doc_url(query: NewsDiscoveryQuery) -> str:
    """构造 DOC 2.0 artlist 请求 URL：只允许固定参数集合。"""
    params = {
        "query": query.query_text,
        "mode": "artlist",
        "format": "json",
        "sort": "datedesc",
        "maxrecords": str(query.max_results),
        "startdatetime": query.start_at.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
        "enddatetime": query.end_at.astimezone(UTC).strftime("%Y%m%d%H%M%S"),
    }
    return f"{API_ENDPOINT}?{urlencode(params)}"


@dataclass(frozen=True)
class GdeltCapturedPayload:
    """一次成功 JSON 响应：解析后的 payload + 原始字节捕获（同一份 HTTP 响应）。"""

    payload: object
    raw_response: NewsRawDiscoveryResponse


class GdeltDocClient:
    """GDELT DOC 2.0 artlist 安全客户端（每次 discover 恰好一次请求）。"""

    def __init__(
        self,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
    ) -> None:
        self._transport = transport
        self._timeout = timeout or _TIMEOUT

    async def discover(self, query: NewsDiscoveryQuery) -> GdeltCapturedPayload:
        url = _build_doc_url(query)
        current = url
        redirects = 0
        try:
            async with self._new_client() as client:
                while True:
                    started = time.monotonic()
                    async with client.stream("GET", current, follow_redirects=False) as response:
                        status = response.status_code
                        if status in _REDIRECT_CODES:
                            location = response.headers.get("location")
                            if not location:
                                raise GdeltRequestFailed("redirect without location")
                            redirects += 1
                            if redirects > _MAX_REDIRECTS:
                                raise GdeltRequestFailed("redirect limit exceeded")
                            next_url = str(httpx.URL(current).join(location))
                            if httpx.URL(next_url).host != API_HOST:
                                raise GdeltRequestFailed("cross-host redirect")
                            if next_url == current:
                                raise GdeltRequestFailed("redirect loop")
                            current = next_url
                            self._log("redirect", status, started)
                            continue
                        body = await self._read_limited(response)
                        if status == 429:
                            self._log("ok", status, started)
                            raise GdeltRequestFailed("rate limited")
                        if status >= 500:
                            self._log("ok", status, started)
                            raise GdeltRequestFailed("upstream error")
                        if not 200 <= status < 300:
                            self._log("ok", status, started)
                            raise GdeltRequestFailed(f"http status {status}")
                        self._validate_content_type(response.headers.get("content-type"))
                        payload = self._parse_json(body)
                        self._log("ok", status, started)
                        raw_response = NewsRawDiscoveryResponse(
                            response_status=status,
                            final_hostname=httpx.URL(current).host or "",
                            content_type=response.headers.get("content-type") or "",
                            fetched_at=datetime.now(UTC),
                            raw_bytes=body,
                        )
                        return GdeltCapturedPayload(
                            payload=payload,
                            raw_response=raw_response,
                        )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            hostname = httpx.URL(current).host or ""
            get_logger("app.news.gdelt").error(
                "request_failed",
                provider_key="gdelt_doc",
                hostname=hostname,
                error_type=type(exc).__name__,
            )
            raise GdeltRequestFailed("GDELT API request failed") from exc

    @staticmethod
    def _validate_content_type(content_type: str | None) -> None:
        if content_type is None:
            raise GdeltInvalidContentType("missing content-type")
        media = content_type.split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise GdeltInvalidContentType(f"unexpected content-type {media}")

    @staticmethod
    async def _read_limited(response: httpx.Response) -> bytes:
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                await response.aclose()
                raise GdeltResponseTooLarge(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _reject_constant(token: str) -> Decimal:
        """拒绝 JSON 的 NaN / Infinity / -Infinity 字面量。"""
        raise GdeltInvalidJson(f"non-finite literal {token!r}")

    @staticmethod
    def _parse_json(body: bytes) -> object:
        try:
            # parse_float=Decimal：JSON number 直接构造 Decimal，禁止先转 float；
            # parse_constant 显式拒绝 NaN / Infinity / -Infinity 字面量。
            return json.loads(
                body.decode("utf-8-sig"),
                parse_float=Decimal,
                parse_constant=GdeltDocClient._reject_constant,
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GdeltInvalidJson("invalid json") from exc

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _log(self, event: str, status: int, started: float) -> None:
        duration_ms = int((time.monotonic() - started) * 1000)
        # 只记录 engine / hostname / status / duration_ms，不记录完整 URL / query。
        get_logger("app.news.gdelt").info(
            event,
            provider_key=NewsDiscoveryEngine.GDELT_DOC.value,
            hostname=API_HOST,
            status=status,
            duration_ms=duration_ms,
        )
