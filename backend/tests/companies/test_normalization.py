"""Tests for company query normalization."""

import pytest

from app.companies.normalization import normalize_company_text, parse_company_query
from app.core.errors import InvalidCompanyQuery
from app.domain.companies import ExchangeCode


def test_nfkc_and_whitespace_folding() -> None:
    assert normalize_company_text("  贵州  茅台  ") == "贵州 茅台"


def test_casefold() -> None:
    assert normalize_company_text("KWEICHOW MOUTAI") == "kweichow moutai"


def test_identity_key_sse() -> None:
    parsed = parse_company_query("SSE:600519")
    assert parsed.identity_key == "SSE:600519"
    assert parsed.explicit_exchange == ExchangeCode.SSE
    assert parsed.security_code == "600519"


def test_identity_key_szse_bse() -> None:
    assert parse_company_query("SZSE:000001").identity_key == "SZSE:000001"
    assert parse_company_query("BSE:430047").identity_key == "BSE:430047"


def test_symbol_sh() -> None:
    parsed = parse_company_query("600519.SH")
    assert parsed.identity_key == "SSE:600519"
    assert parsed.explicit_symbol is True


def test_symbol_sz_bj() -> None:
    assert parse_company_query("000001.SZ").identity_key == "SZSE:000001"
    assert parse_company_query("430047.BJ").identity_key == "BSE:430047"


def test_bare_code_has_no_exchange() -> None:
    parsed = parse_company_query("600519")
    assert parsed.security_code == "600519"
    assert parsed.explicit_exchange is None
    assert parsed.identity_key is None


def test_chinese_name() -> None:
    parsed = parse_company_query("贵州茅台")
    assert parsed.normalized == "贵州茅台"
    assert parsed.security_code is None


def test_english_name() -> None:
    parsed = parse_company_query("Kweichow Moutai")
    assert parsed.normalized == "kweichow moutai"


def test_empty_rejected() -> None:
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("   ")
    with pytest.raises(InvalidCompanyQuery):
        normalize_company_text("")


def test_non_six_digit_numeric_rejected() -> None:
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("60051")
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("1234567")


def test_unknown_explicit_prefix_rejected() -> None:
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("FOO:123456")


def test_unknown_symbol_suffix_rejected() -> None:
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("600519.XS")


def test_too_long_rejected() -> None:
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("x" * 201)
