"""Tests for company query normalization."""

import pytest

from app.companies.normalization import normalize_company_text, parse_company_query
from app.core.errors import InvalidCompanyQuery
from app.domain.companies import ExchangeCode


def test_nfkc_and_whitespace_folding() -> None:
    assert normalize_company_text("  贵州  茅台  ") == "贵州茅台"


def test_casefold() -> None:
    assert normalize_company_text("KWEICHOW MOUTAI") == "kweichowmoutai"


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
    assert parsed.normalized == "kweichowmoutai"


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


def test_whitespace_stripped_from_chinese() -> None:
    """P1 generalization: ALL whitespace stripped, not just collapsed."""
    assert normalize_company_text("五 粮 液") == "五粮液"
    assert normalize_company_text("宁德 时代") == "宁德时代"
    assert normalize_company_text(" 贵 州 茅台 ") == "贵州茅台"


def test_whitespace_stripped_from_official_name() -> None:
    """Whitespace in official names from source data does not block matching."""
    assert normalize_company_text("宜宾五粮液股份有限公司") == "宜宾五粮液股份有限公司"
    assert normalize_company_text("宜宾 五粮液 股份 有限公司") == "宜宾五粮液股份有限公司"


def test_whitespace_stripped_from_security_code() -> None:
    """Security codes with whitespace normalized correctly."""
    result = parse_company_query(" 300750 ")
    assert result.security_code == "300750"
    result2 = parse_company_query(" 000858 .SZ ")
    assert result2 is not None


def test_too_long_rejected() -> None:
    with pytest.raises(InvalidCompanyQuery):
        parse_company_query("x" * 201)


# ------------------------------------------------------------------ P3.3 名称+代码组合


def test_combined_name_and_code_splits_both_orders() -> None:
    """「名称+代码」组合查询：名称放前或放后都拆成 name_text + security_code。"""
    for query in ("贵州茅台600519", "600519贵州茅台"):
        parsed = parse_company_query(query)
        assert parsed.name_text == "贵州茅台"
        assert parsed.security_code == "600519"
        assert parsed.identity_key is None
        assert parsed.explicit_exchange is None
        assert parsed.explicit_symbol is False
        assert parsed.original == query


def test_combined_name_and_code_with_spaces() -> None:
    """名称与代码间有空白也能拆分（NFKC 后按 token 切分）。"""
    parsed = parse_company_query("贵州茅台 600519")
    assert parsed.name_text == "贵州茅台"
    assert parsed.security_code == "600519"


def test_combined_english_name_and_code() -> None:
    parsed = parse_company_query("Kweichow Moutai 600519")
    assert parsed.name_text == "kweichowmoutai"
    assert parsed.security_code == "600519"


def test_pure_forms_have_no_name_text() -> None:
    assert parse_company_query("贵州茅台").name_text is None
    assert parse_company_query("600519").name_text is None
    assert parse_company_query("SSE:600519").name_text is None
    assert parse_company_query("600519.SH").name_text is None
