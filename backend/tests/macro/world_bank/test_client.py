"""World Bank HTTP client unit tests (MockTransport only, stage 2C.1)."""

import json as _json
from decimal import Decimal

import httpx
import pytest

from app.macro.world_bank.client import (
    MAX_RESPONSE_BYTES,
    PER_PAGE,
    WorldBankClient,
    _build_country_url,
    _build_indicator_url,
    _build_observations_url,
)
from app.macro.world_bank.errors import (
    WorldBankApiError,
    WorldBankInvalidContentType,
    WorldBankInvalidJson,
    WorldBankRequestFailed,
    WorldBankRequestLimitExceeded,
    WorldBankResponseTooLarge,
)
from tests.macro.world_bank.helpers import (
    QUERY,
    country_response,
    indicator_response,
    json_response,
    observation_row,
    observations_response,
)

ALLOWED = ["worldbank.org"]

pytestmark = pytest.mark.asyncio


def _client(
    handler,
    *,
    allowed_domains: list[str] | None = None,
    request_limit: int | None = None,
) -> WorldBankClient:
    kwargs: dict = {"allowed_domains": allowed_domains or ALLOWED}
    if request_limit is not None:
        kwargs["request_limit"] = request_limit
    return WorldBankClient(transport=httpx.MockTransport(handler), **kwargs)


# --- URL construction ---


async def test_build_indicator_url():
    assert (
        _build_indicator_url("SP.POP.TOTL") == "https://api.worldbank.org/v2/indicator/SP.POP.TOTL"
    )


async def test_build_country_url():
    assert _build_country_url("CHN") == "https://api.worldbank.org/v2/country/CHN"


async def test_build_observations_url():
    url = _build_observations_url(QUERY, page=2, per_page=PER_PAGE)
    assert url == (
        "https://api.worldbank.org/v2/country/CHN/indicator/SP.POP.TOTL"
        "?format=json&source=2&date=2020:2024&page=2&per_page=1000"
    )


# --- request shape: fixed v2, fixed source, allowlist ---


def _ok_router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v2/indicator/SP.POP.TOTL":
        return json_response(indicator_response())
    if path == "/v2/country/CHN":
        return json_response(country_response())
    if "/v2/country/CHN/indicator/" in path:
        return json_response(observations_response(rows=[observation_row(2020, value=1)]))
    raise AssertionError(f"unexpected path {path}")


async def test_observations_request_shape():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return json_response(observations_response(rows=[observation_row(2020, value=1)]))

    client = _client(handler)
    await client.fetch_observations(QUERY, page=1, per_page=PER_PAGE)
    assert seen["path"] == "/v2/country/CHN/indicator/SP.POP.TOTL"
    assert seen["params"]["source"] == "2"
    assert seen["params"]["format"] == "json"
    assert seen["params"]["date"] == "2020:2024"
    assert seen["params"]["page"] == "1"
    assert seen["params"]["per_page"] == "1000"
    assert client.request_count == 1


async def test_indicator_and_country_metadata_urls():
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        return json_response(country_response())

    client = _client(handler)
    await client.fetch_indicator_metadata("SP.POP.TOTL")
    await client.fetch_country_metadata("CHN")
    assert paths == ["/v2/indicator/SP.POP.TOTL", "/v2/country/CHN"]
    assert client.request_count == 2


async def test_non_allowlist_rejected_without_request():
    client = _client(_ok_router, allowed_domains=["example.org"])
    with pytest.raises(WorldBankRequestFailed):
        await client.fetch_observations(QUERY, page=1)
    assert client.request_count == 0


async def test_non_https_rejected():
    client = _client(_ok_router)
    with pytest.raises(WorldBankRequestFailed):
        client._validate_url("http://api.worldbank.org/v2/indicator/SP.POP.TOTL")
    assert client.request_count == 0


# --- redirect handling ---


async def test_same_domain_redirect_followed():
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if state["count"] == 0:
            state["count"] += 1
            # 同域名重定向到不同 URL（page=2），避免触发 redirect loop 检测。
            location = (
                "/v2/country/CHN/indicator/SP.POP.TOTL"
                "?format=json&source=2&date=2020:2024&page=2&per_page=1000"
            )
            return httpx.Response(302, headers={"location": location})
        return json_response(observations_response(rows=[observation_row(2020, value=1)]))

    client = _client(handler)
    await client.fetch_observations(QUERY, page=1)
    assert state["count"] == 1
    assert client.request_count == 2


async def test_cross_domain_redirect_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "https://evil.example.com/steal"})

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed):
        await client.fetch_observations(QUERY, page=1)
    assert client.request_count == 1


async def test_redirect_loop_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": str(request.url)})

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed):
        await client.fetch_observations(QUERY, page=1)
    assert client.request_count == 1


async def test_redirect_limit_exceeded():
    state = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        # 每跳生成不同 query 的 location，避免命中 redirect-loop 检测。
        state["count"] += 1
        location = f"/v2/indicator/SP.POP.TOTL?format=json&source=2&r={state['count']}"
        return httpx.Response(302, headers={"location": location})

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed):
        await client.fetch_observations(QUERY, page=1)
    assert client.request_count == 4  # 原始请求 + 3 次重定向


async def test_redirect_without_location_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302)

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed):
        await client.fetch_observations(QUERY, page=1)
    assert client.request_count == 1


# --- status / content-type / json / size / timeout ---


@pytest.mark.parametrize("status", [400, 429, 500, 503])
async def test_non_2xx_raises_api_error(status: int):
    client = _client(
        lambda request: httpx.Response(
            status, content=b"{}", headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(WorldBankApiError) as exc:
        await client.fetch_observations(QUERY, page=1)
    assert "status" in str(exc.value)


async def test_non_json_content_type_rejected():
    client = _client(
        lambda request: httpx.Response(
            200, content=b"<html>not json</html>", headers={"content-type": "text/html"}
        )
    )
    with pytest.raises(WorldBankInvalidContentType):
        await client.fetch_observations(QUERY, page=1)


async def test_missing_content_type_rejected():
    client = _client(lambda request: httpx.Response(200, content=b"{}"))
    with pytest.raises(WorldBankInvalidContentType):
        await client.fetch_observations(QUERY, page=1)


async def test_malformed_json_rejected():
    client = _client(
        lambda request: httpx.Response(
            200, content=b"not json", headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(WorldBankInvalidJson):
        await client.fetch_observations(QUERY, page=1)


async def test_response_too_large_rejected():
    big = b"x" * (MAX_RESPONSE_BYTES + 1024)
    client = _client(
        lambda request: httpx.Response(
            200, content=big, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(WorldBankResponseTooLarge):
        await client.fetch_observations(QUERY, page=1)


async def test_read_timeout_maps_to_request_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed) as exc:
        await client.fetch_observations(QUERY, page=1)
    # 稳定错误消息：不泄漏底层 hostname / query / TLS 细节。
    assert str(exc.value) == "World Bank API request failed"


async def test_connect_error_maps_to_request_failed():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed) as exc:
        await client.fetch_observations(QUERY, page=1)
    assert str(exc.value) == "World Bank API request failed"


async def test_json_numbers_parsed_as_decimal():
    payload = observations_response(rows=[observation_row(2020, value=123.45)])
    body = _json.dumps(payload).encode("utf-8")
    client = _client(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
    )
    data = await client.fetch_observations(QUERY, page=1)
    value = data[1][0]["value"]
    assert isinstance(value, Decimal)
    assert value == Decimal("123.45")


@pytest.mark.parametrize("literal", ["NaN", "Infinity", "-Infinity"])
async def test_json_non_finite_literals_rejected(literal: str):
    # parse_constant 显式拒绝 NaN / Infinity / -Infinity 字面量。
    body = f'[{{"page":1,"pages":1,"per_page":50,"total":1}},[{{"value": {literal}}}]]'.encode()
    client = _client(
        lambda request: httpx.Response(
            200, content=body, headers={"content-type": "application/json"}
        )
    )
    with pytest.raises(WorldBankInvalidJson):
        await client.fetch_observations(QUERY, page=1)


# --- security posture: no auth / cookies, trust_env false, request limit ---


async def test_no_auth_cookie_api_key_sent():
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["header_names"] = {key.lower() for key in request.headers}
        return json_response(observations_response(rows=[observation_row(2020, value=1)]))

    client = _client(handler)
    await client.fetch_observations(QUERY, page=1)
    names = seen["header_names"]
    assert "authorization" not in names
    assert "cookie" not in names
    assert "x-api-key" not in names
    assert "api-key" not in names


async def test_trust_env_disabled():
    client = _client(_ok_router)
    async_client = client._new_client()
    try:
        assert async_client.trust_env is False
    finally:
        await async_client.aclose()


async def test_request_limit_enforced():
    client = _client(
        lambda request: json_response(observations_response(rows=[])),
        request_limit=2,
    )
    await client.fetch_observations(QUERY, page=1)
    await client.fetch_observations(QUERY, page=1)
    with pytest.raises(WorldBankRequestLimitExceeded):
        await client.fetch_observations(QUERY, page=1)
    assert client.request_count == 2


# --- log redaction ---


class _RecordingLogger:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.records.append((event, kwargs))

    def error(self, event: str, **kwargs: object) -> None:
        self.records.append((event, kwargs))


async def test_log_redaction(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr("app.macro.world_bank.client.get_logger", lambda name: recorder)
    client = _client(_ok_router)
    await client.fetch_observations(QUERY, page=1)
    assert recorder.records
    event, kwargs = recorder.records[0]
    assert event == "ok"
    assert kwargs["hostname"] == "api.worldbank.org"
    assert kwargs["operation"] == "observations"
    assert kwargs["page"] == 1
    for key in ("url", "query", "body", "data"):
        assert key not in kwargs


async def test_error_log_redaction(monkeypatch):
    recorder = _RecordingLogger()
    monkeypatch.setattr("app.macro.world_bank.client.get_logger", lambda name: recorder)

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client(handler)
    with pytest.raises(WorldBankRequestFailed):
        await client.fetch_observations(QUERY, page=1)
    assert recorder.records
    event, kwargs = recorder.records[0]
    assert event == "request_failed"
    assert kwargs["error_type"] == "ConnectError"
    assert kwargs["hostname"] == "api.worldbank.org"
    assert kwargs["operation"] == "observations"
    assert kwargs["page"] == 1
    # 错误日志可含 error_type/operation/hostname，但不得含完整 URL / query。
    for key in ("url", "query", "body"):
        assert key not in kwargs


async def test_client_construction_does_not_set_httpx_level(monkeypatch):
    import logging

    calls: list[tuple[str, int]] = []
    original = logging.Logger.setLevel

    def spy(self, level: int) -> None:
        calls.append((self.name, level))
        return original(self, level)

    monkeypatch.setattr(logging.Logger, "setLevel", spy)
    _client(_ok_router)
    assert [name for name, _level in calls if name == "httpx"] == []


async def test_configure_logging_sets_httpx_warning():
    import logging

    from app.core.logging import configure_logging

    httpx_logger = logging.getLogger("httpx")
    root = logging.getLogger()
    previous_level = httpx_logger.level
    previous_root_level = root.level
    previous_handlers = list(root.handlers)
    try:
        configure_logging()
        assert httpx_logger.level >= logging.WARNING
    finally:
        httpx_logger.setLevel(previous_level)
        root.setLevel(previous_root_level)
        root.handlers.clear()
        root.handlers.extend(previous_handlers)
