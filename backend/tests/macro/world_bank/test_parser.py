"""World Bank response parser unit tests (stage 2C.1)."""

from decimal import Decimal

import pytest

from app.macro.contracts import (
    MacroGeography,
    MacroGeographyType,
    MacroIndicator,
    MacroPageInfo,
    MacroPeriodSemantics,
)
from app.macro.world_bank.errors import (
    WorldBankApiError,
    WorldBankGeographyNotCountry,
    WorldBankMalformedResponse,
)
from app.macro.world_bank.parser import (
    parse_geography,
    parse_indicator,
    parse_observations,
    parse_page_info,
    split_response,
)
from tests.macro.world_bank.helpers import (
    COUNTRY_ID,
    INDICATOR_ID,
    QUERY,
    country_response,
    indicator_response,
    observation_row,
    observations_response,
    page_header,
)


def _geo() -> MacroGeography:
    return parse_geography(country_response(), requested_code="CHN")


# --- split_response / top-level structure ---


def test_split_response_rejects_non_list():
    with pytest.raises(WorldBankMalformedResponse):
        split_response({"page": 1})


def test_split_response_rejects_wrong_length():
    with pytest.raises(WorldBankMalformedResponse):
        split_response([page_header()])
    with pytest.raises(WorldBankMalformedResponse):
        split_response([page_header(), [], []])


def test_split_response_rejects_non_dict_metadata():
    with pytest.raises(WorldBankMalformedResponse):
        split_response(["meta", []])


def test_split_response_null_rows_allowed():
    metadata, rows = split_response([page_header(total=0), None])
    assert rows is None
    assert metadata["total"] == 0


def test_split_response_api_error_object():
    error_meta = {"page": 1, "pages": 1, "per_page": 50, "total": 0, "message": "not found"}
    with pytest.raises(WorldBankApiError):
        split_response([error_meta, []])


def test_split_response_ok_passes():
    metadata, rows = split_response([page_header(total=1), [{}]])
    assert metadata["total"] == 1
    assert rows == [{}]


# --- parse_page_info ---


def test_parse_page_info_string_numbers():
    info = parse_page_info({"page": "1", "pages": "1", "per_page": "50", "total": "5"})
    assert info.page == 1
    assert info.pages == 1
    assert info.per_page == 50
    assert info.total == 5
    assert info.last_updated is None


def test_parse_page_info_last_updated():
    info = parse_page_info(page_header(lastupdated="2026-01-01"))
    assert isinstance(info, MacroPageInfo)
    assert info.last_updated == "2026-01-01"


@pytest.mark.parametrize(
    "metadata",
    [
        {"page": 0, "pages": 1, "per_page": 50, "total": 0},
        {"page": 1, "pages": 0, "per_page": 50, "total": 0},
        {"page": 2, "pages": 1, "per_page": 50, "total": 0},
        {"page": 1, "pages": 1, "per_page": 50, "total": -1},
        {"page": "abc", "pages": 1, "per_page": 50, "total": 0},
        {"page": True, "pages": 1, "per_page": 50, "total": 0},
        {"page": 1, "pages": 1, "per_page": 50, "total": False},
    ],
)
def test_parse_page_info_invalid(metadata: dict):
    with pytest.raises(WorldBankMalformedResponse):
        parse_page_info(metadata)


# --- parse_indicator ---


def test_parse_indicator_ok():
    indicator = parse_indicator(
        indicator_response(),
        indicator_code=INDICATOR_ID,
        provider_key="world_bank",
    )
    assert isinstance(indicator, MacroIndicator)
    assert indicator.external_indicator_id == INDICATOR_ID
    assert indicator.source_id == "2"
    assert indicator.source_name == "World Development Indicators"
    assert indicator.source_organization == "World Bank"
    assert indicator.topics[0].topic_id == "19"


def test_parse_indicator_id_mismatch():
    raw = [page_header(total=1), [{"id": "OTHER.ID", "source": {"id": "2"}}]]
    with pytest.raises(WorldBankMalformedResponse):
        parse_indicator(raw, indicator_code=INDICATOR_ID, provider_key="world_bank")


def test_parse_indicator_source_mismatch():
    raw = [page_header(total=1), [{"id": INDICATOR_ID, "source": {"id": "1"}}]]
    with pytest.raises(WorldBankMalformedResponse):
        parse_indicator(raw, indicator_code=INDICATOR_ID, provider_key="world_bank")


def test_parse_indicator_no_rows():
    raw = [page_header(total=0), []]
    with pytest.raises(WorldBankMalformedResponse):
        parse_indicator(raw, indicator_code=INDICATOR_ID, provider_key="world_bank")


# --- parse_geography ---


def test_parse_geography_ok():
    geo = _geo()
    assert geo.geography_type == MacroGeographyType.COUNTRY
    assert geo.requested_code == "CHN"
    assert geo.provider_country_id == COUNTRY_ID
    assert geo.iso2_code == "CN"
    assert geo.iso3_code == COUNTRY_ID
    assert geo.name == "China"
    assert geo.region_name == "East Asia & Pacific"
    assert geo.income_level_name == "Upper middle income"


def test_parse_geography_country_id_missing():
    raw = [page_header(total=1), [{"iso2Code": "CN", "name": "China"}]]
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


def test_parse_geography_no_rows():
    raw = [page_header(total=0), []]
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


def _aggregate_response(country_id: str, *, region: object) -> list:
    row = {
        "id": country_id,
        "iso2Code": "CN",
        "name": "Aggregate",
        "region": region,
        "incomeLevel": {"id": "", "value": ""},
    }
    return [page_header(total=1), [row]]


@pytest.mark.parametrize("country_id", ["WLD", "LCN", "HIC"])
def test_aggregate_geographies_rejected(country_id: str):
    # 地区 / 收入组 / 贷款组：region.value == "Aggregates" → geography_not_country。
    raw = _aggregate_response(
        country_id,
        region={"id": "NA", "value": "Aggregates"},
    )
    with pytest.raises(WorldBankGeographyNotCountry):
        parse_geography(raw, requested_code=country_id)


def test_aggregate_region_value_case_insensitive():
    raw = _aggregate_response("WLD", region={"id": "NA", "value": "aggregates"})
    with pytest.raises(WorldBankGeographyNotCountry):
        parse_geography(raw, requested_code="WLD")


def test_aggregate_region_value_empty_rejected():
    # region 存在但 value 缺失/空：保守拒绝，不得错误标记为 country。
    raw = _aggregate_response("WLD", region={"id": "NA", "value": ""})
    with pytest.raises(WorldBankGeographyNotCountry):
        parse_geography(raw, requested_code="WLD")
    raw_missing = _aggregate_response("WLD", region={})
    with pytest.raises(WorldBankGeographyNotCountry):
        parse_geography(raw_missing, requested_code="WLD")


def test_aggregate_region_missing_is_malformed():
    # region 字段缺失/非 object：结构性 malformed response。
    raw = _aggregate_response("WLD", region=None)
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="WLD")


# --- parse_geography: country metadata 字段约束（2C.1.2 §二） ---


def _country_row(
    *,
    country_id: str = "CHN",
    iso2: str = "CN",
    name: str = "China",
    region: object | None = None,
) -> list:
    row = {
        "id": country_id,
        "iso2Code": iso2,
        "name": name,
        "region": region if region is not None else {"id": "EAS", "value": "East Asia & Pacific"},
        "incomeLevel": {"id": "UMC", "value": "Upper middle income"},
    }
    return [page_header(total=1), [row]]


@pytest.mark.parametrize("bad_id", ["CH", "CHNA", "CH1", "cH", ""])
def test_geography_country_id_invalid(bad_id: str):
    raw = _country_row(country_id=bad_id)
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


def test_geography_country_id_non_string():
    raw = _country_row(country_id=123)
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


@pytest.mark.parametrize("bad_iso2", ["C", "CNN", "C1", "c", ""])
def test_geography_iso2_invalid(bad_iso2: str):
    raw = _country_row(iso2=bad_iso2)
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CN")


def test_geography_iso2_non_string():
    raw = _country_row(iso2=12)
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CN")


def test_geography_iso2_missing():
    raw = _country_row()
    del raw[1][0]["iso2Code"]
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CN")


def test_geography_name_missing():
    raw = _country_row()
    del raw[1][0]["name"]
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


def test_geography_name_empty_or_blank():
    raw = _country_row(name="   ")
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


def test_geography_name_non_string():
    raw = _country_row(name=123)
    with pytest.raises(WorldBankMalformedResponse):
        parse_geography(raw, requested_code="CHN")


def test_geography_fields_whitespace_normalized():
    # 字段前后空白规范化：country_id/iso2Code/name 去空白并大写，requested_code 同样规范化。
    raw = _country_row(country_id=" CHN ", iso2=" cn ", name=" China ")
    geo = parse_geography(raw, requested_code="chn")
    assert geo.requested_code == "CHN"
    assert geo.provider_country_id == "CHN"
    assert geo.iso2_code == "CN"
    assert geo.iso3_code == "CHN"
    assert geo.name == "China"


# --- parse_geography: 响应国家与请求一致（2C.1.2 §三） ---


def test_geography_iso2_request_matches_iso2():
    # 两字母请求（CN）必须等于 iso2Code。
    geo = parse_geography(country_response(), requested_code="CN")
    assert geo.requested_code == "CN"
    assert geo.iso2_code == "CN"
    assert geo.iso3_code == "CHN"


def test_geography_iso3_request_matches_country_id():
    # 三字母请求（CHN）必须等于 country id（即 iso3）。
    geo = parse_geography(country_response(), requested_code="CHN")
    assert geo.requested_code == "CHN"
    assert geo.provider_country_id == "CHN"
    assert geo.iso3_code == "CHN"


def test_geography_us_request_matches():
    # US 请求 → 匹配 iso2Code=US；provider_country_id=USA（iso3）。
    raw = _country_row(country_id="USA", iso2="US", name="United States")
    geo = parse_geography(raw, requested_code="US")
    assert geo.requested_code == "US"
    assert geo.provider_country_id == "USA"
    assert geo.iso2_code == "US"
    assert geo.iso3_code == "USA"


def test_geography_iso2_request_rejects_country_mismatch():
    # CN 请求但响应返回 USA/US：国家不匹配 → malformed_response，不猜测国家映射。
    raw = _country_row(country_id="USA", iso2="US", name="United States")
    with pytest.raises(WorldBankMalformedResponse, match="does not match"):
        parse_geography(raw, requested_code="CN")


def test_geography_iso3_request_rejects_country_mismatch():
    # CHN 请求但响应返回 USA/US：国家不匹配 → malformed_response。
    raw = _country_row(country_id="USA", iso2="US", name="United States")
    with pytest.raises(WorldBankMalformedResponse, match="does not match"):
        parse_geography(raw, requested_code="CHN")


# --- parse_observations: value semantics ---


def _parse_single(value: object) -> list:
    _, obs = parse_observations(
        observations_response(rows=[observation_row(2020, value=value)]),
        query=QUERY,
        geography=_geo(),
        provider_key="world_bank",
    )
    return obs


def test_parse_int_value():
    obs = _parse_single(123)
    assert obs[0].value == Decimal(123)
    assert obs[0].is_missing is False
    assert obs[0].decimal_scale == 0


def test_parse_numeric_string():
    obs = _parse_single("123.45")
    assert obs[0].value == Decimal("123.45")
    assert obs[0].decimal_scale == 2


def test_parse_decimal_value():
    obs = _parse_single(Decimal("1.2500"))
    assert obs[0].value == Decimal("1.2500")
    assert obs[0].decimal_scale == 4


def test_parse_null_value_is_missing():
    obs = _parse_single(None)
    assert obs[0].value is None
    assert obs[0].is_missing is True
    assert obs[0].decimal_scale is None
    assert obs[0].period == "2020"
    assert obs[0].normalized_period_start.isoformat() == "2020-01-01"
    assert obs[0].period_semantics is MacroPeriodSemantics.PROVIDER_YEAR_LABEL
    assert obs[0].frequency.value == "annual"


@pytest.mark.parametrize(
    "bad",
    [1.5, True, "", "NaN", "Infinity", "abc", [], {"v": 1}, Decimal("NaN")],
)
def test_parse_invalid_values(bad: object):
    with pytest.raises(WorldBankMalformedResponse):
        _parse_single(bad)


# --- parse_observations: consistency with query ---


def test_observation_indicator_mismatch():
    row = observation_row(2020, value=1, indicator="OTHER.ID")
    with pytest.raises(WorldBankMalformedResponse):
        parse_observations(
            observations_response(rows=[row]),
            query=QUERY,
            geography=_geo(),
            provider_key="world_bank",
        )


def test_observation_country_id_mismatch():
    row = observation_row(2020, value=1, country="USA")
    with pytest.raises(WorldBankMalformedResponse):
        parse_observations(
            observations_response(rows=[row]),
            query=QUERY,
            geography=_geo(),
            provider_key="world_bank",
        )


def test_observation_iso3_mismatch():
    row = observation_row(2020, value=1, iso3="USA")
    with pytest.raises(WorldBankMalformedResponse):
        parse_observations(
            observations_response(rows=[row]),
            query=QUERY,
            geography=_geo(),
            provider_key="world_bank",
        )


def test_observation_date_out_of_range_low():
    row = observation_row(2019, value=1)
    with pytest.raises(WorldBankMalformedResponse):
        parse_observations(
            observations_response(rows=[row]),
            query=QUERY,
            geography=_geo(),
            provider_key="world_bank",
        )


def test_observation_date_out_of_range_high():
    row = observation_row(2025, value=1)
    with pytest.raises(WorldBankMalformedResponse):
        parse_observations(
            observations_response(rows=[row]),
            query=QUERY,
            geography=_geo(),
            provider_key="world_bank",
        )


@pytest.mark.parametrize("bad_date", ["20244", "abcd", "20", "2020-01"])
def test_observation_invalid_date(bad_date: str):
    row = observation_row(2020, value=1)
    row["date"] = bad_date
    with pytest.raises(WorldBankMalformedResponse):
        parse_observations(
            observations_response(rows=[row]),
            query=QUERY,
            geography=_geo(),
            provider_key="world_bank",
        )


def test_observation_status_preserved():
    _, obs = parse_observations(
        observations_response(rows=[observation_row(2020, value=1, obs_status="E")]),
        query=QUERY,
        geography=_geo(),
        provider_key="world_bank",
    )
    assert obs[0].observation_status == "E"


def test_observation_status_empty_to_none():
    _, obs = parse_observations(
        observations_response(rows=[observation_row(2020, value=1, obs_status="")]),
        query=QUERY,
        geography=_geo(),
        provider_key="world_bank",
    )
    assert obs[0].observation_status is None


def test_parse_observations_empty_rows():
    page_info, obs = parse_observations(
        observations_response(rows=[]),
        query=QUERY,
        geography=_geo(),
        provider_key="world_bank",
    )
    assert obs == []
    assert page_info.total == 5


def test_parse_observations_null_rows():
    page_info, obs = parse_observations(
        observations_response(rows=None),
        query=QUERY,
        geography=_geo(),
        provider_key="world_bank",
    )
    assert obs == []
    assert page_info.pages == 1
