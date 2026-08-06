"""Company identity domain enums."""

from enum import StrEnum


class ExchangeCode(StrEnum):
    SSE = "SSE"
    SZSE = "SZSE"
    BSE = "BSE"


class MarketBoard(StrEnum):
    SSE_MAIN = "sse_main"
    STAR = "star"
    SZSE_MAIN = "szse_main"
    CHINEXT = "chinext"
    BSE = "bse"


class CompanyListingStatus(StrEnum):
    LISTED = "listed"
    DELISTED = "delisted"
    UNKNOWN = "unknown"


class CompanyAliasType(StrEnum):
    OFFICIAL_NAME = "official_name"
    SHORT_NAME = "short_name"
    FORMER_NAME = "former_name"
    ENGLISH_NAME = "english_name"


class CompanyMatchType(StrEnum):
    IDENTITY_KEY = "identity_key"
    EXPLICIT_SYMBOL = "explicit_symbol"
    SECURITY_CODE = "security_code"
    OFFICIAL_NAME = "official_name"
    SHORT_NAME = "short_name"
    FORMER_NAME = "former_name"
    ENGLISH_NAME = "english_name"
