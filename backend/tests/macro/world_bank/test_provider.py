"""World Bank provider snapshot & fetch tests (MockTransport only, stage 2C.1)."""

import httpx
import pytest

from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)
from app.macro.contracts import MacroQuery
from app.macro.world_bank.errors import (
    WorldBankGeographyNotCountry,
    WorldBankMalformedResponse,
    WorldBankProviderNotReady,
)
from tests.macro.world_bank.helpers import (
    QUERY,
    country_response,
    indicator_response,
    json_response,
    make_provider_row,
    observation_row,
    observations_response,
    page_header,
)

pytestmark = pytest.mark.asyncio


def _ok_router(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/v2/indicator/SP.POP.TOTL":
        return json_response(indicator_response())
    if path == "/v2/country/CHN":
        return json_response(country_response())
    if "/v2/country/CHN/indicator/" in path:
        page = int(request.url.params["page"])
        rows = [observation_row(year, value=index) for index, year in enumerate(range(2020, 2025))]
        return json_response(
            observations_response(page=page, pages=1, per_page=1000, total=len(rows), rows=rows)
        )
    raise AssertionError(f"unexpected path {path}")


# --- provider-not-ready: registry configuration ---


async def test_provider_not_found(world_bank_provider):
    provider, _factory = world_bank_provider(row=None)
    with pytest.raises(WorldBankProviderNotReady) as exc:
        await provider.fetch(QUERY)
    assert exc.value.code == "provider_not_ready"


async def test_provider_disabled(world_bank_provider):
    provider, _factory = world_bank_provider(row=make_provider_row(enabled=False))
    with pytest.raises(WorldBankProviderNotReady):
        await provider.fetch(QUERY)


async def test_provider_missing_macro_data_capability(world_bank_provider):
    provider, _factory = world_bank_provider(
        row=make_provider_row(capabilities=["document_download"])
    )
    with pytest.raises(WorldBankProviderNotReady):
        await provider.fetch(QUERY)


async def test_provider_missing_official_api_method(world_bank_provider):
    provider, _factory = world_bank_provider(
        row=make_provider_row(acquisition_methods=["official_web_page"])
    )
    with pytest.raises(WorldBankProviderNotReady):
        await provider.fetch(QUERY)


async def test_provider_requires_api_key(world_bank_provider):
    provider, _factory = world_bank_provider(row=make_provider_row(requires_api_key=True))
    with pytest.raises(WorldBankProviderNotReady):
        await provider.fetch(QUERY)


# --- snapshot semantics ---


async def test_provider_snapshot_fields(world_bank_provider):
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_ok_router))
    result = await provider.fetch(QUERY)
    assert result.provider_key == "world_bank"
    assert result.authority_tier == SourceAuthorityTier.TIER_1
    assert result.critical_claim_eligible is True
    assert result.acquisition_method == AcquisitionMethod.OFFICIAL_API
    assert result.provider_capabilities == (
        SourceCapability.DOCUMENT_DOWNLOAD,
        SourceCapability.MACRO_DATA,
    )
    assert result.source_id == "2"


async def test_provider_unknown_capabilities_ignored(world_bank_provider):
    row = make_provider_row(capabilities=["macro_data", "document_download", "not_a_capability"])
    provider, _factory = world_bank_provider(row=row, transport=httpx.MockTransport(_ok_router))
    result = await provider.fetch(QUERY)
    assert result.provider_capabilities == (
        SourceCapability.DOCUMENT_DOWNLOAD,
        SourceCapability.MACRO_DATA,
    )


async def test_session_closed_during_network(world_bank_provider):
    closed_during_network: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        closed_during_network["closed"] = factory.closed
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CHN":
            return json_response(country_response())
        return json_response(
            observations_response(page=1, pages=1, per_page=1000, total=0, rows=[])
        )

    provider, factory = world_bank_provider(transport=httpx.MockTransport(handler))
    await provider.fetch(QUERY)
    assert closed_during_network["closed"] is True


async def test_fetch_only_reads_registry_once(world_bank_provider):
    provider, factory = world_bank_provider(transport=httpx.MockTransport(_ok_router))
    result = await provider.fetch(QUERY)
    # 网络 I/O 期间不持有 Session；Registry 只读一次、不写数据库。
    assert factory.session is not None
    assert factory.session.executes == 1
    assert result.request_count == 3


# --- full fetch combo ---


async def test_full_fetch_combo(world_bank_provider):
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_ok_router))
    result = await provider.fetch(QUERY)
    assert result.indicator.external_indicator_id == "SP.POP.TOTL"
    assert result.indicator.source_id == "2"
    assert result.geography.iso3_code == "CHN"
    assert result.geography.geography_type.value == "country"
    assert [o.period for o in result.observations] == ["2020", "2021", "2022", "2023", "2024"]


async def test_observations_ascending(world_bank_provider):
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_ok_router))
    result = await provider.fetch(QUERY)
    starts = [o.normalized_period_start for o in result.observations]
    assert starts == sorted(starts)
    assert all(
        result.observations[i].normalized_period_start
        <= result.observations[i + 1].normalized_period_start
        for i in range(len(result.observations) - 1)
    )


async def test_observation_semantics_fields(world_bank_provider):
    provider, _factory = world_bank_provider(transport=httpx.MockTransport(_ok_router))
    result = await provider.fetch(QUERY)
    for obs in result.observations:
        assert obs.period_semantics.value == "provider_year_label"
        assert obs.normalized_period_start.isoformat() == f"{obs.period}-01-01"


# --- geography: single-country constraint ---


async def test_country_two_letter_resolves(world_bank_provider):
    # 两字母 ISO2 请求（CN）应解析到 CHN 国家：provider_country_id=CHN、iso2=CN、iso3=CHN。
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CN":
            return json_response(country_response())
        if "/v2/country/CN/indicator/" in path:
            return json_response(observations_response(rows=[]))
        raise AssertionError(f"unexpected path {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    query = MacroQuery(
        provider_key="world_bank",
        indicator_code="SP.POP.TOTL",
        country_code="CN",
        start_year=2020,
        end_year=2024,
    )
    result = await provider.fetch(query)
    assert result.geography.requested_code == "CN"
    assert result.geography.provider_country_id == "CHN"
    assert result.geography.iso2_code == "CN"
    assert result.geography.iso3_code == "CHN"


async def test_aggregate_geography_rejected_before_observations(world_bank_provider):
    # 聚合项（region.value=Aggregates）拒绝后不得继续获取 observations。
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/WLD":
            row = {
                "id": "WLD",
                "iso2Code": "XW",
                "name": "World",
                "region": {"id": "NA", "value": "Aggregates"},
                "incomeLevel": {"id": "", "value": ""},
            }
            return json_response([page_header(total=1), [row]])
        requested_paths.append(path)
        raise AssertionError(f"observations should not be fetched: {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    query = MacroQuery(
        provider_key="world_bank",
        indicator_code="SP.POP.TOTL",
        country_code="WLD",
        start_year=2020,
        end_year=2024,
    )
    with pytest.raises(WorldBankGeographyNotCountry) as exc:
        await provider.fetch(query)
    assert exc.value.code == "geography_not_country"
    assert requested_paths == []


async def test_country_metadata_mismatch_aborts_before_observations(world_bank_provider):
    # 响应国家与请求不一致（请求 CN、响应 USA/US）→ malformed_response；
    # 拒绝后不继续获取 observations，实际发出的请求只有两条元数据请求。
    requested_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        requested_paths.append(path)
        if path == "/v2/indicator/SP.POP.TOTL":
            return json_response(indicator_response())
        if path == "/v2/country/CN":
            row = {
                "id": "USA",
                "iso2Code": "US",
                "name": "United States",
                "region": {"id": "NAC", "value": "North America"},
                "incomeLevel": {"id": "HIC", "value": "High income"},
            }
            return json_response([page_header(total=1), [row]])
        raise AssertionError(f"observations should not be fetched: {path}")

    provider, _factory = world_bank_provider(transport=httpx.MockTransport(handler))
    query = MacroQuery(
        provider_key="world_bank",
        indicator_code="SP.POP.TOTL",
        country_code="CN",
        start_year=2020,
        end_year=2024,
    )
    with pytest.raises(WorldBankMalformedResponse) as exc:
        await provider.fetch(query)
    assert exc.value.code == "malformed_response"
    assert "does not match" in str(exc.value)
    # request_count 只含已发出的元数据请求（indicator + country），未发出观测分页请求。
    assert requested_paths == ["/v2/indicator/SP.POP.TOTL", "/v2/country/CN"]
