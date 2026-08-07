"""Shared fixtures/helpers for World Bank macro tests (MockTransport only)."""

import json
from datetime import UTC, date, datetime
from decimal import Decimal

import httpx

from app.db.models.source_provider import SourceProviderModel
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
    MacroQuery,
    MacroTopic,
)

QUERY = MacroQuery(
    provider_key="world_bank",
    indicator_code="SP.POP.TOTL",
    country_code="CHN",
    start_year=2020,
    end_year=2024,
)
INDICATOR_ID = "SP.POP.TOTL"
COUNTRY_ID = "CHN"
ISO2 = "CN"
ISO3 = "CHN"


def page_header(
    page: int = 1,
    pages: int = 1,
    per_page: int = 50,
    total: int = 5,
    lastupdated: str = "2026-01-01",
) -> dict:
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "total": total,
        "lastupdated": lastupdated,
    }


def indicator_response() -> list:
    return [
        page_header(total=1),
        [
            {
                "id": INDICATOR_ID,
                "name": "Population, total",
                "unit": "",
                "source": {"id": "2", "value": "World Development Indicators"},
                "sourceNote": "Total population is based on the de facto definition.",
                "sourceOrganization": "World Bank",
                "topics": [{"id": "19", "value": "Population: Structure, growth & density"}],
            }
        ],
    ]


def country_response() -> list:
    return [
        page_header(total=1),
        [
            {
                "id": COUNTRY_ID,
                "iso2Code": ISO2,
                "name": "China",
                "region": {"id": "EAS", "value": "East Asia & Pacific"},
                "incomeLevel": {"id": "UMC", "value": "Upper middle income"},
                "lendingType": {"id": "IBD", "value": "IBRD"},
                "capitalCity": "Beijing",
            }
        ],
    ]


def observation_row(
    year: int,
    value: object | None = None,
    obs_status: str = "",
    indicator: str = INDICATOR_ID,
    country: str = COUNTRY_ID,
    iso3: str = ISO3,
) -> dict:
    return {
        "indicator": {"id": indicator, "value": "Population, total"},
        "country": {"id": country, "value": "China"},
        "countryiso3code": iso3,
        "date": str(year),
        "value": value,
        "unit": "",
        "obs_status": obs_status,
        "decimal": 0,
    }


def observations_response(
    page: int = 1,
    pages: int = 1,
    per_page: int = 50,
    total: int = 5,
    rows: list[dict] | None = None,
) -> list:
    return [page_header(page=page, pages=pages, per_page=per_page, total=total), rows]


def json_response(payload: object, status: int = 200) -> httpx.Response:
    # default=str：mock 载荷中的 Decimal 序列化为数字字符串，保留精度；
    # 客户端 parse_float=Decimal / 数字字符串路径均能还原。
    body = json.dumps(payload, default=str).encode("utf-8")
    return httpx.Response(status, content=body, headers={"content-type": "application/json"})


def make_provider_row(
    *,
    enabled: bool = True,
    capabilities: list[str] | None = None,
    acquisition_methods: list[str] | None = None,
    requires_api_key: bool = False,
    critical_claim_eligible: bool = True,
    authority_tier: int = 1,
    allowed_domains: list[str] | None = None,
) -> SourceProviderModel:
    """构造内存中的 SourceProviderModel（不持久化），供 Provider 快照测试使用。"""
    return SourceProviderModel(
        provider_key="world_bank",
        display_name="World Bank Open Data",
        provider_type="international_organization",
        authority_tier=authority_tier,
        homepage_url="https://data.worldbank.org",
        allowed_domains=allowed_domains if allowed_domains is not None else ["worldbank.org"],
        capabilities=(
            capabilities if capabilities is not None else ["macro_data", "document_download"]
        ),
        acquisition_methods=(
            acquisition_methods if acquisition_methods is not None else ["official_api"]
        ),
        exchange_scope=[],
        requires_api_key=requires_api_key,
        critical_claim_eligible=critical_claim_eligible,
        enabled=enabled,
    )


class _FakeResult:
    def __init__(self, row: object) -> None:
        self._row = row

    def scalar_one_or_none(self) -> object:
        return self._row


class _FakeSession:
    def __init__(self, row: object) -> None:
        self._row = row
        self.executes = 0

    async def execute(self, _stmt: object) -> _FakeResult:
        self.executes += 1
        return _FakeResult(self._row)


class _FakeSessionContext:
    """一次 `async with factory()` 生成的 session 上下文。"""

    def __init__(self, owner: "FakeSessionFactory") -> None:
        self._owner = owner
        self.session = _FakeSession(owner._row)

    async def __aenter__(self) -> _FakeSession:
        return self.session

    async def __aexit__(self, *_exc: object) -> None:
        self._owner.closed = True


class FakeSessionFactory:
    """可调用对象，代替 async_sessionmaker（只读 Registry 查询，不连库）。

    provider 调用 `async with self._sessionmaker() as session`，因此本类必须可调用，
    每次调用返回一个新的 async 上下文管理器。
    """

    def __init__(self, row: object) -> None:
        self._row = row
        self.session: _FakeSession | None = None
        self.closed = False

    def __call__(self) -> _FakeSessionContext:
        context = _FakeSessionContext(self)
        self.session = context.session
        return context


def sample_result(*, observations: tuple[MacroObservation, ...] | None = None) -> MacroFetchResult:
    """构造一次完整成功获取结果（CLI 序列化测试用）。"""
    indicator = MacroIndicator(
        provider_key="world_bank",
        external_indicator_id=INDICATOR_ID,
        name="Population, total",
        unit="",
        source_id="2",
        source_name="World Development Indicators",
        source_note="Total population is based on the de facto definition.",
        source_organization="World Bank",
        topics=(MacroTopic(topic_id="19", name="Population: Structure, growth & density"),),
    )
    geography = MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code="CHN",
        provider_country_id="CHN",
        iso2_code="CN",
        iso3_code="CHN",
        name="China",
        region_name="East Asia & Pacific",
        income_level_name="Upper middle income",
    )
    if observations is None:
        observations = tuple(
            MacroObservation(
                provider_key="world_bank",
                external_indicator_id=INDICATOR_ID,
                geography_code="CHN",
                period=str(year),
                period_start=date(year, 1, 1),
                frequency=MacroFrequency.ANNUAL,
                value=Decimal("1410000000"),
                is_missing=False,
                observation_status="",
            )
            for year in range(QUERY.start_year, QUERY.end_year + 1)
        )
    return MacroFetchResult(
        provider_key="world_bank",
        query=QUERY,
        indicator=indicator,
        geography=geography,
        observations=observations,
        page_info=MacroPageInfo(page=1, pages=1, per_page=50, total=len(observations)),
        fetched_at=datetime.now(UTC),
        request_count=3,
        acquisition_method=AcquisitionMethod.OFFICIAL_API,
        authority_tier=SourceAuthorityTier.TIER_1,
        critical_claim_eligible=True,
        provider_capabilities=(SourceCapability.DOCUMENT_DOWNLOAD, SourceCapability.MACRO_DATA),
    )
