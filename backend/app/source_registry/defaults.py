"""Default source provider definitions."""

from pydantic import BaseModel, ConfigDict, Field

from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
    SourceProviderType,
)
from app.source_registry.url_policy import validate_provider_definition


class SourceProviderDefinition(BaseModel):
    model_config = ConfigDict(use_enum_values=False)

    provider_key: str = Field(pattern=r"^[a-z0-9_-]+$", max_length=32)
    display_name: str = Field(max_length=100)
    provider_type: SourceProviderType
    authority_tier: SourceAuthorityTier
    homepage_url: str
    allowed_domains: list[str]
    capabilities: list[SourceCapability]
    acquisition_methods: list[AcquisitionMethod]
    exchange_scope: list[str] = Field(default_factory=list)
    requires_api_key: bool = False
    critical_claim_eligible: bool = False
    enabled: bool = True


def _provider(**kwargs: object) -> SourceProviderDefinition:
    definition = SourceProviderDefinition.model_validate(kwargs)
    validate_provider_definition(definition)
    return definition


DEFAULT_PROVIDERS: list[SourceProviderDefinition] = [
    _provider(
        provider_key="sse",
        display_name="上海证券交易所",
        provider_type=SourceProviderType.EXCHANGE,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://www.sse.com.cn",
        allowed_domains=["sse.com.cn"],
        capabilities=[
            SourceCapability.COMPANY_ANNOUNCEMENT,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["SSE"],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="szse",
        display_name="深圳证券交易所",
        provider_type=SourceProviderType.EXCHANGE,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://www.szse.cn",
        allowed_domains=["szse.cn"],
        capabilities=[
            SourceCapability.COMPANY_ANNOUNCEMENT,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["SZSE"],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="bse",
        display_name="北京证券交易所",
        provider_type=SourceProviderType.EXCHANGE,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://www.bse.cn",
        allowed_domains=["bse.cn"],
        capabilities=[
            SourceCapability.COMPANY_ANNOUNCEMENT,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["BSE"],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="cninfo",
        display_name="巨潮资讯网",
        provider_type=SourceProviderType.STATUTORY_DISCLOSURE_PLATFORM,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://www.cninfo.com.cn",
        allowed_domains=["cninfo.com.cn"],
        capabilities=[
            SourceCapability.COMPANY_SEARCH,
            SourceCapability.COMPANY_ANNOUNCEMENT,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["SSE", "SZSE", "BSE"],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="csrc",
        display_name="中国证券监督管理委员会",
        provider_type=SourceProviderType.REGULATOR,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://www.csrc.gov.cn",
        allowed_domains=["csrc.gov.cn"],
        capabilities=[
            SourceCapability.REGULATION,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["SSE", "SZSE", "BSE"],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="nbs",
        display_name="国家统计局",
        provider_type=SourceProviderType.GOVERNMENT_DATA,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://data.stats.gov.cn",
        allowed_domains=["stats.gov.cn"],
        capabilities=[
            SourceCapability.MACRO_DATA,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=[],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="fred",
        display_name="Federal Reserve Economic Data",
        provider_type=SourceProviderType.AUTHORITATIVE_DATA,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://fred.stlouisfed.org",
        allowed_domains=["stlouisfed.org"],
        capabilities=[SourceCapability.MACRO_DATA],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_API,
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
        ],
        exchange_scope=[],
        requires_api_key=True,
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="world_bank",
        display_name="World Bank Open Data",
        provider_type=SourceProviderType.INTERNATIONAL_ORGANIZATION,
        authority_tier=SourceAuthorityTier.TIER_1,
        homepage_url="https://data.worldbank.org",
        allowed_domains=["worldbank.org"],
        capabilities=[
            SourceCapability.MACRO_DATA,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_API,
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=[],
        critical_claim_eligible=True,
    ),
    _provider(
        provider_key="xinhuanet",
        display_name="新华网",
        provider_type=SourceProviderType.MEDIA,
        authority_tier=SourceAuthorityTier.TIER_3,
        homepage_url="https://www.xinhuanet.com",
        allowed_domains=["xinhuanet.com"],
        capabilities=[SourceCapability.NEWS_ARTICLE],
        acquisition_methods=[AcquisitionMethod.PUBLIC_HTML],
        exchange_scope=[],
        critical_claim_eligible=False,
    ),
    _provider(
        provider_key="cnstock",
        display_name="上海证券报·中国证券网",
        provider_type=SourceProviderType.MEDIA,
        authority_tier=SourceAuthorityTier.TIER_3,
        homepage_url="https://www.cnstock.com",
        allowed_domains=["cnstock.com"],
        capabilities=[SourceCapability.NEWS_ARTICLE],
        acquisition_methods=[AcquisitionMethod.PUBLIC_HTML],
        exchange_scope=[],
        critical_claim_eligible=False,
    ),
    _provider(
        provider_key="cs_com_cn",
        display_name="中国证券报·中证网",
        provider_type=SourceProviderType.MEDIA,
        authority_tier=SourceAuthorityTier.TIER_3,
        homepage_url="https://www.cs.com.cn",
        allowed_domains=["cs.com.cn"],
        capabilities=[SourceCapability.NEWS_ARTICLE],
        acquisition_methods=[AcquisitionMethod.PUBLIC_HTML],
        exchange_scope=[],
        critical_claim_eligible=False,
    ),
]
