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


def _special_provider(**kwargs: object) -> SourceProviderDefinition:
    """不经过 validate_provider_definition 的特殊定义。

    仅用于 allowed_domains 为空的 provider（域名由运行时 registry 动态决定，
    不属于固定 allowlist）：
    - issuer_official：域名来自 issuer_domains registry（公司官网域名）；
    - user_supplied：用户转录来源，source_url 只是 provenance 文本且服务端
      从不抓取（SSRF 面为零），不做域名 allowlist 校验。
    """
    return SourceProviderDefinition.model_validate(kwargs)


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
    # East Money 公告 API（V1.1 closure）：CNINFO WAF 不可用时的受控 Tier-3
    # 后备（同 BSE 后备先例）。API 主机 np-anotice-stock.eastmoney.com /
    # np-cnotice-stock.eastmoney.com ∈ eastmoney.com；PDF 主机 pdf.dfcfw.com
    # ∈ dfcfw.com。只用于**自动发现**与文件下载，critical_claim_eligible=False
    # （Tier-3 平台，不直接支撑关键主张）。
    _provider(
        provider_key="eastmoney",
        display_name="东方财富（公告数据中心）",
        provider_type=SourceProviderType.PROFESSIONAL_MEDIA,
        authority_tier=SourceAuthorityTier.TIER_3,
        homepage_url="https://www.eastmoney.com",
        allowed_domains=["eastmoney.com", "dfcfw.com"],
        capabilities=[
            SourceCapability.COMPANY_ANNOUNCEMENT,
            SourceCapability.ISSUER_IR,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.AUTOMATIC_DISCOVERY,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["SSE", "SZSE", "BSE"],
        critical_claim_eligible=False,
    ),
    # 上市公司官网（issuer_official）：域名由 issuer_domains registry 动态
    # 决定（公司官网域名，company_id 绑定 + 真实验证 URL），不属于固定
    # allowlist → 特殊 seed。Tier-2（公司官方披露，但未经交易所/监管核验）。
    _special_provider(
        provider_key="issuer_official",
        display_name="上市公司官方网站",
        provider_type=SourceProviderType.ISSUER,
        authority_tier=SourceAuthorityTier.TIER_2,
        # 占位主页：本 provider 无单一 homepage，域名验证走 issuer_domains。
        homepage_url="https://example.com",
        allowed_domains=[],
        capabilities=[
            SourceCapability.ISSUER_IR,
            SourceCapability.DOCUMENT_DOWNLOAD,
        ],
        acquisition_methods=[
            AcquisitionMethod.OFFICIAL_WEB_PAGE,
            AcquisitionMethod.OFFICIAL_FILE_DOWNLOAD,
        ],
        exchange_scope=["SSE", "SZSE", "BSE"],
        critical_claim_eligible=True,
    ),
    # 用户转录（user_supplied）：用户从官方报告/官网人工转录的财务数值
    # 证据来源（V1.1 closure）。Tier-4、critical_claim_eligible=False；
    # source_url 只是 provenance 文本（服务端从不抓取，无 SSRF 面）。
    _special_provider(
        provider_key="user_supplied",
        display_name="用户转录（官方报告）",
        provider_type=SourceProviderType.GENERAL_WEB,
        authority_tier=SourceAuthorityTier.TIER_4,
        homepage_url="https://example.com",
        allowed_domains=[],
        capabilities=[],
        acquisition_methods=[AcquisitionMethod.USER_SUPPLIED],
        exchange_scope=["SSE", "SZSE", "BSE"],
        critical_claim_eligible=False,
    ),
]
