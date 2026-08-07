"""Macro contract unit tests (stage 2C.1)."""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)
from app.macro.contracts import (
    MacroFetchResult,
    MacroFrequency,
    MacroGeography,
    MacroGeographyType,
    MacroIndicator,
    MacroObservation,
    MacroPageInfo,
    MacroPeriodSemantics,
    MacroQuery,
    current_macro_year,
    decimal_scale_of,
)

CURRENT_YEAR = current_macro_year()


def _query(**overrides: object) -> MacroQuery:
    base: dict[str, object] = {
        "provider_key": "world_bank",
        "indicator_code": "SP.POP.TOTL",
        "country_code": "CHN",
        "start_year": 2020,
        "end_year": 2024,
    }
    base.update(overrides)
    return MacroQuery(**base)  # type: ignore[arg-type]


# --- MacroQuery: provider / indicator / country ---


def test_provider_key_must_be_world_bank():
    with pytest.raises(ValueError):
        _query(provider_key="fred")


def test_country_normalized_to_upper():
    assert _query(country_code="chn").country_code == "CHN"
    assert _query(country_code="cN").country_code == "CN"


@pytest.mark.parametrize("code", ["", "ALL", "all", "All", "USAJ", "US1", "C", "U S A"])
def test_invalid_country_code(code: str):
    with pytest.raises(ValueError):
        _query(country_code=code)


def test_valid_indicator_codes():
    for code in ["SP.POP.TOTL", "NY.GDP.MKTP.CD", "A1.B-C_2", "X"]:
        assert _query(indicator_code=code).indicator_code == code


@pytest.mark.parametrize("code", ["", "sp.pop.totl", "SP POP", "SP?POP", "X" * 65])
def test_invalid_indicator_code(code: str):
    with pytest.raises(ValueError):
        _query(indicator_code=code)


# --- MacroQuery: years ---


def test_year_lower_bound_ok():
    assert _query(start_year=1960, end_year=1960).start_year == 1960


def test_year_below_min_rejected():
    with pytest.raises(ValueError):
        _query(start_year=1959, end_year=1960)


def test_current_year_upper_bound_ok():
    assert _query(start_year=CURRENT_YEAR, end_year=CURRENT_YEAR).end_year == CURRENT_YEAR


def test_future_year_rejected():
    with pytest.raises(ValueError):
        _query(start_year=CURRENT_YEAR + 1, end_year=CURRENT_YEAR + 1)


def test_start_after_end_rejected():
    with pytest.raises(ValueError):
        _query(start_year=2024, end_year=2023)


def test_span_exactly_60_allowed():
    query = _query(start_year=1960, end_year=2019)
    assert query.end_year - query.start_year + 1 == 60


def test_span_over_60_rejected():
    with pytest.raises(ValueError):
        _query(start_year=1960, end_year=2020)  # 闭区间 61 年


# --- MacroObservation: value / is_missing / decimal_scale ---


def _obs(
    period: str = "2020",
    value: Decimal | None = None,
    is_missing: bool | None = None,
    *,
    provider_key: str = "world_bank",
    external_indicator_id: str = "SP.POP.TOTL",
    geography_code: str = "CHN",
):
    if is_missing is None:
        is_missing = value is None
    return MacroObservation(
        provider_key=provider_key,
        external_indicator_id=external_indicator_id,
        geography_code=geography_code,
        period=period,
        normalized_period_start=date(int(period), 1, 1),
        frequency=MacroFrequency.ANNUAL,
        value=value,
        is_missing=is_missing,
    )


def test_missing_requires_flag():
    with pytest.raises(ValueError):
        _obs(value=None, is_missing=False)


def test_value_conflicts_with_missing():
    with pytest.raises(ValueError):
        _obs(value=Decimal("1"), is_missing=True)


def test_value_rejects_float():
    with pytest.raises(ValueError):
        _obs(value=1.5, is_missing=False)


@pytest.mark.parametrize(
    "bad",
    [Decimal("Infinity"), Decimal("-Infinity"), Decimal("NaN")],
)
def test_value_rejects_non_finite(bad: Decimal):
    with pytest.raises(ValueError):
        _obs(value=bad, is_missing=False)


def test_period_must_be_4_digit_year():
    with pytest.raises(ValueError):
        _obs(period="20244")
    with pytest.raises(ValueError):
        _obs(period="abcd")


def test_normalized_period_start_must_be_jan_1():
    with pytest.raises(ValueError):
        MacroObservation(
            provider_key="world_bank",
            external_indicator_id="SP.POP.TOTL",
            geography_code="CHN",
            period="2020",
            normalized_period_start=date(2020, 7, 1),
            frequency=MacroFrequency.ANNUAL,
            value=None,
            is_missing=True,
        )


def test_period_semantics_defaults_to_provider_year_label():
    obs = _obs()
    assert obs.period_semantics is MacroPeriodSemantics.PROVIDER_YEAR_LABEL
    assert obs.normalized_period_start == date(2020, 1, 1)


def test_period_semantics_rejects_other_values():
    with pytest.raises(ValueError):
        MacroObservation(
            provider_key="world_bank",
            external_indicator_id="SP.POP.TOTL",
            geography_code="CHN",
            period="2020",
            normalized_period_start=date(2020, 1, 1),
            frequency=MacroFrequency.ANNUAL,
            value=None,
            is_missing=True,
            period_semantics="custom_semantics",  # type: ignore[arg-type]
        )


def test_period_semantics_enum_value():
    assert MacroPeriodSemantics.PROVIDER_YEAR_LABEL.value == "provider_year_label"


def test_decimal_scale():
    assert decimal_scale_of(Decimal("123")) == 0
    assert decimal_scale_of(Decimal("1.5")) == 1
    assert decimal_scale_of(Decimal("1.2500")) == 4


def test_observation_decimal_scale_auto():
    assert _obs(value=Decimal("1.2500")).decimal_scale == 4
    assert _obs(value=None).decimal_scale is None


def test_frequency_and_geography_enums():
    assert MacroFrequency.ANNUAL.value == "annual"
    assert MacroGeographyType.COUNTRY.value == "country"


# --- MacroFetchResult: ordering / snapshot fields ---


def _indicator() -> MacroIndicator:
    return MacroIndicator(
        provider_key="world_bank",
        external_indicator_id="SP.POP.TOTL",
        name="Population, total",
        unit="",
        source_id="2",
        source_name="World Development Indicators",
        source_note="",
        source_organization="World Bank",
    )


def _geography() -> MacroGeography:
    return MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code="CHN",
        provider_country_id="CHN",
        iso2_code="CN",
        iso3_code="CHN",
        name="China",
    )


def _result(
    observations: list[MacroObservation],
    **overrides: object,
) -> MacroFetchResult:
    base: dict[str, object] = {
        "provider_key": "world_bank",
        "query": _query(),
        "indicator": _indicator(),
        "geography": _geography(),
        "observations": tuple(observations),
        "page_info": MacroPageInfo(page=1, pages=1, per_page=50, total=len(observations)),
        "fetched_at": datetime.now(UTC),
        "request_count": 3,
        "acquisition_method": AcquisitionMethod.OFFICIAL_API,
        "authority_tier": SourceAuthorityTier.TIER_1,
        "critical_claim_eligible": True,
        "provider_capabilities": (
            SourceCapability.MACRO_DATA,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ),
    }
    base.update(overrides)
    return MacroFetchResult(**base)  # type: ignore[arg-type]


def test_observations_sorted_ascending():
    o2022 = _obs(period="2022", value=Decimal("3"))
    o2020 = _obs(period="2020", value=Decimal("1"))
    o2021 = _obs(period="2021", value=Decimal("2"))
    result = _result([o2022, o2020, o2021])
    assert [o.period for o in result.observations] == ["2020", "2021", "2022"]


def test_same_period_stable_order():
    first = _obs(period="2020", value=Decimal("1"))
    second = _obs(period="2020", value=Decimal("1"))
    result = _result([second, first])
    assert result.observations[0] is second
    assert result.observations[1] is first


def test_capabilities_sorted():
    result = _result(
        [_obs()],
        provider_capabilities=(
            SourceCapability.MACRO_DATA,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ),
    )
    assert result.provider_capabilities == (
        SourceCapability.DOCUMENT_DOWNLOAD,
        SourceCapability.MACRO_DATA,
    )


def test_fetch_result_default_source_id():
    assert _result([_obs()]).source_id == "2"


def test_fetch_result_rejects_wrong_source_id():
    with pytest.raises(ValueError):
        _result([_obs()], source_id="1")


def test_fetch_result_indicator_source_id_must_match():
    mismatch = _indicator()
    object.__setattr__(mismatch, "source_id", "11")
    with pytest.raises(ValueError):
        _result([_obs()], indicator=mismatch)


def test_fetch_result_indicator_source_id_consistent():
    result = _result([_obs()])
    assert result.indicator.source_id == result.source_id == "2"


def test_fetch_result_requires_official_api():
    with pytest.raises(ValueError):
        _result([_obs()], acquisition_method=AcquisitionMethod.OFFICIAL_WEB_PAGE)


def test_fetch_result_rejects_negative_request_count():
    with pytest.raises(ValueError):
        _result([_obs()], request_count=-1)


def test_fetch_result_rejects_non_tuple_observations():
    with pytest.raises(ValueError):
        _result([_obs()]).__class__(
            provider_key="world_bank",
            query=_query(),
            indicator=_indicator(),
            geography=_geography(),
            observations=[_obs()],
            page_info=MacroPageInfo(page=1, pages=1, per_page=50, total=1),
            fetched_at=datetime.now(UTC),
            request_count=3,
            acquisition_method=AcquisitionMethod.OFFICIAL_API,
            authority_tier=SourceAuthorityTier.TIER_1,
            critical_claim_eligible=True,
            provider_capabilities=(SourceCapability.MACRO_DATA,),
        )


# --- MacroFetchResult: 跨对象一致性（2C.1.2 §四/§五） ---


def test_fetch_result_valid_china_ok():
    """正常 CHN 请求：query/indicator/geography/observation 全部一致则成功。"""
    result = _result(
        [
            _obs(period="2020", value=Decimal("1")),
            _obs(period="2021", value=Decimal("2"), is_missing=False),
        ]
    )
    assert result.query.country_code == "CHN"
    assert result.geography.requested_code == "CHN"
    assert [o.geography_code for o in result.observations] == ["CHN", "CHN"]


def test_fetch_result_iso2_request_matches():
    """ISO2 请求：query=CN + geography.requested_code=CN 合法，
    observation.geography_code 为 iso3 的 CHN（不要求与 query 直接相等）。"""
    query = _query(country_code="CN")
    geo = MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code="CN",
        provider_country_id="CHN",
        iso2_code="CN",
        iso3_code="CHN",
        name="China",
    )
    result = _result([_obs()], query=query, geography=geo)
    assert result.query.country_code == "CN"
    assert result.geography.requested_code == "CN"
    assert result.observations[0].geography_code == "CHN"


def test_fetch_result_result_provider_key_mismatch():
    with pytest.raises(ValueError, match="provider_key 必须跨"):
        _result([_obs()], provider_key="fred")


def test_fetch_result_indicator_provider_key_mismatch():
    indicator = MacroIndicator(
        provider_key="fred",
        external_indicator_id="SP.POP.TOTL",
        name="Population, total",
        unit="",
        source_id="2",
        source_name="World Development Indicators",
        source_note="",
        source_organization="World Bank",
    )
    with pytest.raises(ValueError, match="provider_key 必须跨"):
        _result([_obs()], indicator=indicator)


def test_fetch_result_observation_provider_key_mismatch():
    obs = _obs(provider_key="fred")
    with pytest.raises(ValueError, match="provider_key 必须跨"):
        _result([obs])


def test_fetch_result_query_indicator_code_mismatch():
    query = _query(indicator_code="NY.GDP.MKTP.CD")
    with pytest.raises(ValueError, match="indicator 必须与 query/observation"):
        _result([_obs()], query=query)


def test_fetch_result_observation_indicator_mismatch():
    obs = _obs(external_indicator_id="NY.GDP.MKTP.CD")
    with pytest.raises(ValueError, match="indicator 必须与 query/observation"):
        _result([obs])


def test_fetch_result_geography_requested_mismatch():
    geo = MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code="USA",
        provider_country_id="USA",
        iso2_code="US",
        iso3_code="USA",
        name="United States",
    )
    with pytest.raises(ValueError, match="geography 必须与 query"):
        _result([_obs()], geography=geo)


def test_fetch_result_observation_geography_mismatch():
    obs = _obs(geography_code="USA")
    with pytest.raises(ValueError, match="geography 必须与 query"):
        _result([obs])


def test_fetch_result_observation_before_start_year():
    obs = _obs(period="2019")
    with pytest.raises(ValueError, match="observation.period"):
        _result([obs])


def test_fetch_result_observation_after_end_year():
    obs = _obs(period="2025")
    with pytest.raises(ValueError, match="observation.period"):
        _result([obs])


def test_fetch_result_observation_frequency_mismatch():
    # 合法 MacroObservation 必然 frequency=annual；用显式对象构造后的字段改写
    # 模拟未来 FRED 月度/季度频率进入当前 annual-only 契约时被拒绝。
    obs = _obs()
    object.__setattr__(obs, "frequency", "monthly")  # type: ignore[attr-defined]
    with pytest.raises(ValueError, match="observation.frequency"):
        _result([obs])
