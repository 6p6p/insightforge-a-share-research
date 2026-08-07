"""Unit tests for the GDELT DOC 2.0 secure client (stage 2D.1).

覆盖 §九/§十：固定 endpoint 与参数、UTC 时间格式、单次请求、无
Cookie/Auth/API key、trust_env=false、手动 redirect（同 host 跟随 / 跨 host
拒绝 / 上限 / 环）、429/5xx/超时稳定错误、Content-Type 校验、5 MiB 上限、
非法 JSON 拒绝（含 NaN）、日志脱敏字段白名单。全部使用 MockTransport，
Network Guard（conftest autouse）继续生效。
"""

from datetime import UTC, datetime
from uuid import UUID

import httpx
import pytest
import structlog
import structlog.testing

from app.news.contracts import NewsDiscoveryQuery
from app.news.gdelt.client import (
    GdeltDocClient,
    _build_doc_url,
)
from app.news.gdelt.errors import (
    GdeltInvalidContentType,
    GdeltInvalidJson,
    GdeltRequestFailed,
    GdeltResponseTooLarge,
)

_COMPANY_ID = UUID("11111111-2222-3333-4444-555555555555")
_QUERY = NewsDiscoveryQuery(
    company_id=_COMPANY_ID,
    query_text="Kweichow Moutai",
    start_at=datetime(2026, 8, 1, tzinfo=UTC),
    end_at=datetime(2026, 8, 6, 12, 30, 45, tzinfo=UTC),
    max_results=10,
)

_JSON_HEADERS = {"content-type": "application/json"}


def _response(status: int = 200, json_body: object | None = None, **kwargs) -> httpx.Response:
    if json_body is not None and "content" not in kwargs:
        kwargs["json"] = json_body
    headers = kwargs.pop("headers", _JSON_HEADERS)
    return httpx.Response(status, headers=headers, **kwargs)


def _client(handler) -> GdeltDocClient:
    return GdeltDocClient(transport=httpx.MockTransport(handler))


# ---------------------------------------------------------------- endpoint / 参数


def test_fixed_endpoint_and_params() -> None:
    url = _build_doc_url(_QUERY)
    assert url.startswith("https://api.gdeltproject.org/api/v2/doc/doc?")
    params = dict(httpx.URL(url).params)
    assert params["mode"] == "artlist"
    assert params["format"] == "json"
    assert params["sort"] == "datedesc"
    assert params["maxrecords"] == "10"
    assert params["query"] == "Kweichow Moutai"
    # UTC YYYYMMDDHHMMSS
    assert params["startdatetime"] == "20260801000000"
    assert params["enddatetime"] == "20260806123045"


def test_request_is_single_and_fixed() -> None:
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return _response(json_body={"articles": []})

    client = _client(handler)
    import asyncio

    asyncio.run(client.discover(_QUERY))
    assert len(seen) == 1
    assert "api.gdeltproject.org/api/v2/doc/doc" in seen[0]


def test_no_auth_cookie_headers_and_trust_env_false() -> None:
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.update(dict(request.headers))
        return _response(json_body={"articles": []})

    import asyncio

    client = _client(handler)
    asyncio.run(client.discover(_QUERY))
    assert "authorization" not in {k.lower() for k in captured_headers}
    assert "cookie" not in {k.lower() for k in captured_headers}
    assert "api-key" not in {k.lower() for k in captured_headers}

    # trust_env=false：网络 I/O 不读系统代理/环境变量。
    async def _check() -> bool:
        async with client._new_client() as c:
            return c.trust_env

    assert asyncio.run(_check()) is False


# ---------------------------------------------------------------- redirect 安全


def test_same_host_redirect_followed() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if len(calls) == 1:
            return httpx.Response(
                302,
                headers={"location": "/api/v2/doc/doc?mode=artlist&format=json"},
            )
        return _response(json_body={"articles": []})

    import asyncio

    captured = asyncio.run(_client(handler).discover(_QUERY))
    assert len(calls) == 2
    assert captured.raw_response.response_status == 200


def test_cross_host_redirect_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/x"})

    import asyncio

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(_client(handler).discover(_QUERY))
    assert "cross-host redirect" in str(exc.value)


def test_redirect_limit_exceeded() -> None:
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(len(calls))
        return httpx.Response(302, headers={"location": f"/x{calls[-1]}"})

    import asyncio

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(_client(handler).discover(_QUERY))
    assert "redirect limit exceeded" in str(exc.value)


def test_redirect_loop_rejected() -> None:
    target = "https://api.gdeltproject.org/api/v2/doc/doc?mode=artlist&format=json"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": target})

    import asyncio

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(_client(handler).discover(_QUERY))
    assert "redirect loop" in str(exc.value)


# ---------------------------------------------------------------- 稳定错误


def test_429_rate_limited() -> None:
    import asyncio

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(
            _client(lambda request: httpx.Response(429, headers=_JSON_HEADERS)).discover(_QUERY)
        )
    assert str(exc.value) == "rate limited"


def test_5xx_upstream_error() -> None:
    import asyncio

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(
            _client(lambda request: httpx.Response(502, headers=_JSON_HEADERS)).discover(_QUERY)
        )
    assert str(exc.value) == "upstream error"


def test_4xx_stable_error() -> None:
    import asyncio

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(
            _client(lambda request: httpx.Response(403, headers=_JSON_HEADERS)).discover(_QUERY)
        )
    assert "http status" in str(exc.value)


def test_timeout_wraps_to_stable_error() -> None:
    import asyncio

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("connection timed out")

    with pytest.raises(GdeltRequestFailed) as exc:
        asyncio.run(_client(handler).discover(_QUERY))
    assert "request failed" in str(exc.value)


def test_invalid_content_type_rejected() -> None:
    import asyncio

    client = _client(
        lambda request: httpx.Response(
            200, json={"articles": []}, headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(GdeltInvalidContentType) as exc:
        asyncio.run(client.discover(_QUERY))
    assert exc.value.code == "gdelt_invalid_content_type"


def test_response_over_5mib_rejected() -> None:
    import asyncio

    huge = b"x" * (5 * 1024 * 1024 + 1)
    client = _client(lambda request: httpx.Response(200, content=huge, headers=_JSON_HEADERS))
    with pytest.raises(GdeltResponseTooLarge) as exc:
        asyncio.run(client.discover(_QUERY))
    assert exc.value.code == "gdelt_response_too_large"


def test_invalid_json_rejected() -> None:
    import asyncio

    client = _client(
        lambda request: httpx.Response(200, content=b"not json", headers=_JSON_HEADERS)
    )
    with pytest.raises(GdeltInvalidJson) as exc:
        asyncio.run(client.discover(_QUERY))
    assert exc.value.code == "gdelt_invalid_json"


def test_nan_literal_rejected() -> None:
    import asyncio

    client = _client(
        lambda request: httpx.Response(
            200, content=b'{"articles":[{"x": NaN}]}', headers=_JSON_HEADERS
        )
    )
    with pytest.raises(GdeltInvalidJson) as exc:
        asyncio.run(client.discover(_QUERY))
    assert exc.value.code == "gdelt_invalid_json"


# ---------------------------------------------------------------- 日志脱敏


def test_log_fields_whitelist() -> None:
    """结构化日志只允许 engine/hostname/status/duration_ms/error_type/result_count/
    request_count；绝不包含完整 query_text / 完整 URL / body。"""
    import asyncio

    with structlog.testing.capture_logs() as events:
        client = _client(lambda request: _response(json_body={"articles": []}))
        asyncio.run(client.discover(_QUERY))
        with pytest.raises(GdeltRequestFailed):
            asyncio.run(
                _client(lambda request: httpx.Response(500, headers=_JSON_HEADERS)).discover(_QUERY)
            )

    assert events, "expected structured log events"
    allowed = {
        "provider_key",
        "hostname",
        "status",
        "duration_ms",
        "error_type",
        "result_count",
        "request_count",
        "logger",
        "event",
        "log_level",
        "level",
        "timestamp",
    }
    for event in events:
        assert set(event.keys()) <= allowed
        for value in event.values():
            assert value != _QUERY.query_text
