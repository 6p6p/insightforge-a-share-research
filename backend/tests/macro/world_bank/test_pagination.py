"""World Bank pagination merge tests (stage 2C.1)."""

from decimal import Decimal

import httpx
import pytest

from app.macro.world_bank.errors import (
    WorldBankMalformedResponse,
    WorldBankRequestLimitExceeded,
    WorldBankResponseConflict,
)
from tests.macro.world_bank.helpers import (
    QUERY,
    country_response,
    indicator_response,
    json_response,
    observation_row,
    observations_response,
)

pytestmark = pytest.mark.asyncio


def _router(rows_by_page: dict[int, list[dict]]) -> object:
    """按 request page 返回对应观测页，同时处理 indicator/country 元数据路由。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            rows = rows_by_page.get(page, [])
            return json_response(
                observations_response(
                    page=page,
                    pages=len(rows_by_page),
                    per_page=1000,
                    total=len(rows_by_page) * 1000,
                    rows=rows,
                )
            )
        raise AssertionError(f"unexpected path {path}")

    return handler


async def test_single_page(world_bank_provider):
    rows = [observation_row(2020, value=Decimal("1")), observation_row(2021, value=Decimal("2"))]
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_router({1: rows})))
    result = await provider.fetch(QUERY)
    assert [o.period for o in result.observations] == ["2020", "2021"]
    assert result.page_info.page == 1
    assert result.page_info.pages == 1
    assert result.request_count == 3  # indicator + country + 1 页观测


async def test_multi_page_merge(world_bank_provider):
    rows_by_page = {
        1: [observation_row(2020, value=Decimal("1")), observation_row(2021, value=Decimal("2"))],
        2: [observation_row(2022, value=Decimal("3")), observation_row(2023, value=Decimal("4"))],
        3: [observation_row(2024, value=Decimal("5"))],
    }
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_router(rows_by_page)))
    result = await provider.fetch(QUERY)
    assert [o.period for o in result.observations] == [
        "2020",
        "2021",
        "2022",
        "2023",
        "2024",
    ]
    assert result.page_info.pages == 3
    assert result.request_count == 5  # indicator + country + 3 页观测


async def test_page_request_order_ascending(world_bank_provider):
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            requested.append(page)
            return json_response(
                observations_response(
                    page=page,
                    pages=3,
                    per_page=1000,
                    total=3,
                    rows=[observation_row(2019 + page, value=Decimal(page))],
                )
            )
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    await provider.fetch(QUERY)
    assert requested == [1, 2, 3]


async def test_pages_exceed_max(world_bank_provider):
    # pages=21 > MAX_OBSERVATION_PAGES=18：第一页即拒绝，不再请求后续页。
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            requested.append(page)
            return json_response(
                observations_response(page=page, pages=21, per_page=1000, total=21000, rows=[])
            )
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    with pytest.raises(WorldBankRequestLimitExceeded):
        await provider.fetch(QUERY)
    assert requested == [1]  # 不继续请求下一页


async def test_pages_exceed_max_rejected_first_page(world_bank_provider):
    # 边界：pages=19 > 18，总请求只到 3（2 元数据 + 首页观测），不超过 20。
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            requested.append(page)
            return json_response(
                observations_response(page=page, pages=19, per_page=1000, total=19000, rows=[])
            )
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    with pytest.raises(WorldBankRequestLimitExceeded) as exc:
        await provider.fetch(QUERY)
    assert exc.value.code == "request_limit_exceeded"
    assert requested == [1]


async def test_pages_at_max_completes_within_budget(world_bank_provider):
    # pages=18：2 + 18 == 20 == REQUEST_LIMIT，恰好完成，总请求不超过 20。
    requested: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            requested.append(page)
            return json_response(
                observations_response(page=page, pages=18, per_page=1000, total=18000, rows=[])
            )
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    result = await provider.fetch(QUERY)
    assert requested == list(range(1, 19))
    assert result.page_info.pages == 18
    assert result.request_count == 20  # 2 元数据 + 18 观测页 == REQUEST_LIMIT


async def test_response_page_mismatch(world_bank_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        return json_response(
            observations_response(page=99, pages=1, per_page=1000, total=1, rows=[])
        )

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    with pytest.raises(WorldBankMalformedResponse):
        await provider.fetch(QUERY)


async def test_pages_grew_across_requests(world_bank_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            pages = 3 if page == 1 else 4
            return json_response(
                observations_response(page=page, pages=pages, per_page=1000, total=4, rows=[])
            )
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    with pytest.raises(WorldBankMalformedResponse):
        await provider.fetch(QUERY)


async def test_request_limit_exceeded(world_bank_provider):
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        if "/v2/country/CHN/indicator/" in path:
            page = int(request.url.params["page"])
            return json_response(
                observations_response(page=page, pages=20, per_page=1000, total=20000, rows=[])
            )
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    with pytest.raises(WorldBankRequestLimitExceeded):
        await provider.fetch(QUERY)


async def test_cross_page_dedupe(world_bank_provider):
    rows_by_page = {
        1: [observation_row(2020, value=Decimal("1")), observation_row(2021, value=Decimal("2"))],
        2: [observation_row(2021, value=Decimal("2")), observation_row(2022, value=Decimal("3"))],
    }
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_router(rows_by_page)))
    result = await provider.fetch(QUERY)
    assert [o.period for o in result.observations] == ["2020", "2021", "2022"]


async def test_cross_page_conflict(world_bank_provider):
    rows_by_page = {
        1: [observation_row(2020, value=Decimal("1")), observation_row(2021, value=Decimal("2"))],
        2: [observation_row(2021, value=Decimal("999")), observation_row(2022, value=Decimal("3"))],
    }
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_router(rows_by_page)))
    with pytest.raises(WorldBankResponseConflict):
        await provider.fetch(QUERY)


async def test_missing_records_preserved_across_pages(world_bank_provider):
    rows_by_page = {
        1: [observation_row(2020, value=None), observation_row(2021, value=Decimal("2"))],
        2: [observation_row(2022, value=None), observation_row(2023, value=Decimal("4"))],
        3: [observation_row(2024, value=Decimal("5"))],
    }
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_router(rows_by_page)))
    result = await provider.fetch(QUERY)
    assert [o.is_missing for o in result.observations] == [True, False, True, False, False]
    assert result.observations[0].value is None
    assert result.observations[2].value is None
