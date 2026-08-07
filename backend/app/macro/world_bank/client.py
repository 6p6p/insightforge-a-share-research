"""Secure HTTP client for the World Bank Indicators API V2.

安全规则：
- 仅 https；host 固定 api.worldbank.org；URL 仍必须通过 Source Registry allowlist；
- 固定 API 版本 v2、固定 source=2（World Development Indicators）；接口模板只在代码内部构造，
  不接受任意 endpoint / 额外 query 参数；
- trust_env=False；不发送 Cookie / Authorization / API Key；不接受用户 Header；
- follow_redirects=False；手动处理同 allowlist 重定向（最多 3 次），跨 allowlist 拒绝；
- 不自动重试；connect/read/write/pool timeout 明确设置；
- 单响应正文上限 5 MiB（流式读取 + 大小限制）；Content-Type 必须为 application/json；
- JSON 解析使用 parse_float=Decimal，禁止先转 float；
- 日志只记录 provider_key、hostname、status、duration_ms、operation、page，
  不记录完整 URL query 与响应正文。
"""

import json
import logging
import time
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import quote

import httpx

from app.core.logging import get_logger
from app.macro.contracts import MacroQuery
from app.macro.world_bank.errors import (
    WorldBankApiError,
    WorldBankInvalidContentType,
    WorldBankInvalidJson,
    WorldBankRequestFailed,
    WorldBankRequestLimitExceeded,
    WorldBankResponseTooLarge,
)
from app.source_registry.url_policy import is_url_allowed

API_HOST = "api.worldbank.org"
API_BASE = "https://api.worldbank.org/v2"
SOURCE_ID = "2"
MAX_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
REQUEST_LIMIT = 20  # 单次 MacroQuery 总请求上限
PER_PAGE = 1000
_MAX_REDIRECTS = 3
_REDIRECT_CODES = (301, 302, 303, 307, 308)

_TIMEOUT = httpx.Timeout(connect=10.0, read=30.0, write=10.0, pool=10.0)


def _safe_segment(value: str) -> str:
    """URL 路径段编码：indicator/country 均为受限 ASCII，防御性编码。"""
    return quote(value, safe="")


def _build_indicator_url(indicator_code: str) -> str:
    return f"{API_BASE}/indicator/{_safe_segment(indicator_code)}"


def _build_country_url(country_code: str) -> str:
    return f"{API_BASE}/country/{_safe_segment(country_code)}"


def _build_observations_url(
    query: MacroQuery,
    *,
    page: int,
    per_page: int,
) -> str:
    base = (
        f"{API_BASE}/country/{_safe_segment(query.country_code)}"
        f"/indicator/{_safe_segment(query.indicator_code)}"
    )
    return (
        f"{base}?format=json&source={SOURCE_ID}"
        f"&date={query.start_year}:{query.end_year}"
        f"&page={page}&per_page={per_page}"
    )


@dataclass(frozen=True)
class WorldBankHttpResult:
    """一次成功的 JSON 响应（已解析）。只承载解析后的对象与分页上下文。"""

    data: object
    operation: str
    page: int | None


class WorldBankClient:
    """World Bank Indicators API V2 安全客户端。

    同一个实例用于一次完整查询；构造时接收 Provider 快照的 allowed_domains。
    """

    def __init__(
        self,
        *,
        allowed_domains: list[str],
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: httpx.Timeout | None = None,
        request_limit: int = REQUEST_LIMIT,
    ) -> None:
        # httpx 默认在 INFO 级记录完整请求 URL（含 query），与脱敏约束冲突；静默之。
        logging.getLogger("httpx").setLevel(logging.WARNING)
        self._allowed_domains = list(allowed_domains)
        self._transport = transport
        self._timeout = timeout or _TIMEOUT
        self._request_limit = request_limit
        self._request_count = 0

    @property
    def request_count(self) -> int:
        return self._request_count

    def _next_request(self) -> None:
        if self._request_count >= self._request_limit:
            raise WorldBankRequestLimitExceeded(
                f"request count {self._request_count} exceeds limit {self._request_limit}"
            )
        self._request_count += 1

    def _validate_url(self, url: str) -> None:
        if not is_url_allowed(url, self._allowed_domains):
            raise WorldBankRequestFailed("url not allowed")

    async def fetch_indicator_metadata(self, indicator_code: str) -> object:
        url = _build_indicator_url(indicator_code) + f"?format=json&source={SOURCE_ID}"
        return await self._request_json(url, operation="indicator")

    async def fetch_country_metadata(self, country_code: str) -> object:
        url = _build_country_url(country_code) + "?format=json"
        return await self._request_json(url, operation="country")

    async def fetch_observations(
        self,
        query: MacroQuery,
        *,
        page: int,
        per_page: int = PER_PAGE,
    ) -> object:
        url = _build_observations_url(query, page=page, per_page=per_page)
        return await self._request_json(url, operation="observations", page=page)

    async def _request_json(
        self,
        url: str,
        *,
        operation: str,
        page: int | None = None,
    ) -> object:
        self._validate_url(url)
        current = url
        redirects = 0
        try:
            async with self._new_client() as client:
                while True:
                    self._next_request()
                    started = time.monotonic()
                    async with client.stream("GET", current, follow_redirects=False) as response:
                        status = response.status_code
                        if status in _REDIRECT_CODES:
                            location = response.headers.get("location")
                            if not location:
                                raise WorldBankRequestFailed("redirect without location")
                            redirects += 1
                            if redirects > _MAX_REDIRECTS:
                                raise WorldBankRequestFailed("redirect limit exceeded")
                            next_url = str(httpx.URL(current).join(location))
                            # 同 allowlist 才继续；跨 allowlist 拒绝。
                            self._validate_url(next_url)
                            if next_url == current:
                                raise WorldBankRequestFailed("redirect loop")
                            current = next_url
                            self._log(operation, current, status, started, page, "redirect")
                            continue
                        body = await self._read_limited(response)
                        duration_ms = int((time.monotonic() - started) * 1000)
                        self._log(operation, current, status, started, page, "ok")
                        if not 200 <= status < 300:
                            raise WorldBankApiError(f"http status {status}")
                        self._validate_content_type(response.headers.get("content-type"))
                        return self._parse_json(body, duration_ms)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise WorldBankRequestFailed(str(exc)) from exc

    @staticmethod
    def _validate_content_type(content_type: str | None) -> None:
        if content_type is None:
            raise WorldBankInvalidContentType("missing content-type")
        media = content_type.split(";", 1)[0].strip().lower()
        if media != "application/json":
            raise WorldBankInvalidContentType(f"unexpected content-type {media}")

    @staticmethod
    async def _read_limited(response: httpx.Response) -> bytes:
        total = 0
        chunks: list[bytes] = []
        async for chunk in response.aiter_bytes(chunk_size=65536):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                await response.aclose()
                raise WorldBankResponseTooLarge(f"response exceeds {MAX_RESPONSE_BYTES} bytes")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _parse_json(body: bytes, duration_ms: int) -> object:
        try:
            # parse_float=Decimal：JSON number 直接构造 Decimal，禁止先转 float。
            return json.loads(body.decode("utf-8-sig"), parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorldBankInvalidJson(f"invalid json after {duration_ms}ms") from exc

    def _new_client(self) -> httpx.AsyncClient:
        kwargs: dict = {
            "follow_redirects": False,
            "trust_env": False,
            "timeout": self._timeout,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    def _log(
        self,
        operation: str,
        url: str,
        status: int,
        started: float,
        page: int | None,
        event: str,
    ) -> None:
        hostname = httpx.URL(url).host or ""
        duration_ms = int((time.monotonic() - started) * 1000)
        # 只记录 hostname，不记录完整 URL / query / 响应正文。
        get_logger("app.macro.world_bank").info(
            event,
            provider_key="world_bank",
            hostname=hostname,
            status=status,
            duration_ms=duration_ms,
            operation=operation,
            page=page,
        )
