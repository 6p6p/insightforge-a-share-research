"""Source registry domain enums."""

from enum import IntEnum, StrEnum


class SourceAuthorityTier(IntEnum):
    TIER_1 = 1
    TIER_2 = 2
    TIER_3 = 3
    TIER_4 = 4


class SourceProviderType(StrEnum):
    EXCHANGE = "exchange"
    REGULATOR = "regulator"
    STATUTORY_DISCLOSURE_PLATFORM = "statutory_disclosure_platform"
    ISSUER = "issuer"
    GOVERNMENT_DATA = "government_data"
    AUTHORITATIVE_DATA = "authoritative_data"
    INTERNATIONAL_ORGANIZATION = "international_organization"
    PROFESSIONAL_MEDIA = "professional_media"
    GENERAL_WEB = "general_web"


class SourceCapability(StrEnum):
    COMPANY_DIRECTORY = "company_directory"
    COMPANY_SEARCH = "company_search"
    COMPANY_ANNOUNCEMENT = "company_announcement"
    REGULATION = "regulation"
    ISSUER_IR = "issuer_ir"
    MACRO_DATA = "macro_data"
    NEWS = "news"
    DOCUMENT_DOWNLOAD = "document_download"


class AcquisitionMethod(StrEnum):
    OFFICIAL_API = "official_api"
    OFFICIAL_FILE_DOWNLOAD = "official_file_download"
    OFFICIAL_WEB_PAGE = "official_web_page"
    USER_UPLOAD = "user_upload"
    USER_PROVIDED_URL = "user_provided_url"
    WEB_SEARCH_DISCOVERY = "web_search_discovery"
    MODEL_WEB_SEARCH_DISCOVERY = "model_web_search_discovery"
