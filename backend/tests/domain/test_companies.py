"""Tests for company identity domain enums."""

from app.domain.companies import (
    CompanyAliasType,
    CompanyListingStatus,
    CompanyMatchType,
    ExchangeCode,
    MarketBoard,
)


def test_exchange_codes() -> None:
    assert [exchange.value for exchange in ExchangeCode] == ["SSE", "SZSE", "BSE"]


def test_market_boards() -> None:
    assert [board.value for board in MarketBoard] == [
        "sse_main",
        "star",
        "szse_main",
        "chinext",
        "bse",
    ]


def test_listing_statuses() -> None:
    assert [status.value for status in CompanyListingStatus] == [
        "listed",
        "delisted",
        "unknown",
    ]


def test_alias_types() -> None:
    assert [alias_type.value for alias_type in CompanyAliasType] == [
        "official_name",
        "short_name",
        "former_name",
        "english_name",
    ]


def test_match_types() -> None:
    assert [match.value for match in CompanyMatchType] == [
        "identity_key",
        "explicit_symbol",
        "security_code",
        "official_name",
        "short_name",
        "former_name",
        "english_name",
    ]
