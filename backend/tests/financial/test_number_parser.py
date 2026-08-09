"""Financial number parser + unit normalization unit tests (stage 4B.2A, spec J/K).

零 LLM / 零 Chroma / 零 DB：验证 parse_financial_number 的 v1 严格语法与
Decimal 精确性，以及 normalize_value_cny 的 4 档单位换算（全程无 float）。
"""

from decimal import Decimal

import pytest

from app.financial.errors import (
    FinancialMetricStorageRangeError,
    FinancialMetricValueNotNumeric,
)
from app.financial.number_parser import (
    find_financial_number_tokens,
    normalize_value_cny,
    parse_financial_number,
    validate_financial_decimal_storage,
)


def _tokens(text: str) -> list[tuple[str, int, int]]:
    return [(t.text, t.start, t.end) for t in find_financial_number_tokens(text)]


# ---------------------------------------------------------------- 正例


def test_parse_integer() -> None:
    assert parse_financial_number("123") == Decimal("123")


def test_parse_integer_with_thousands_separator() -> None:
    assert parse_financial_number("1,234") == Decimal("1234")
    assert parse_financial_number("1,234,567") == Decimal("1234567")


def test_parse_decimal() -> None:
    assert parse_financial_number("123.45") == Decimal("123.45")
    assert parse_financial_number("0.5") == Decimal("0.5")


def test_parse_signed() -> None:
    assert parse_financial_number("-123.45") == Decimal("-123.45")
    assert parse_financial_number("+123.45") == Decimal("123.45")


def test_parse_parentheses_is_negative() -> None:
    assert parse_financial_number("(123.45)") == Decimal("-123.45")


def test_parse_leading_trailing_whitespace() -> None:
    assert parse_financial_number("  123  ") == Decimal("123")
    assert parse_financial_number("\t-45\n") == Decimal("-45")


def test_parse_decimal_is_exact_no_float() -> None:
    value = parse_financial_number("0.1")
    assert value == Decimal("0.1")
    assert str(value) == "0.1"  # 不是 0.10000000000000001
    value = parse_financial_number("1.01")
    assert str(value) == "1.01"


def test_parse_returns_decimal_type() -> None:
    assert isinstance(parse_financial_number("999"), Decimal)


# ---------------------------------------------------------------- 反例


def test_parse_rejects_scientific_notation() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("1e3")


def test_parse_rejects_percent() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("12%")


def test_parse_rejects_chinese_numerals() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("一百")


def test_parse_rejects_approximate() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("约100")


def test_parse_rejects_value_with_unit() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("100亿")
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("100万元")


def test_parse_rejects_blank() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("")
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("   ")


def test_parse_rejects_malformed_thousands_separator() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("1,23")
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("1,2345")


def test_parse_rejects_double_decimal() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("12.3.4")


def test_parse_rejects_unbalanced_parenthesis() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("(123")


def test_parse_rejects_parenthesis_with_sign() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("(-123)")


def test_parse_rejects_internal_whitespace() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("1 234")


def test_parse_rejects_non_string() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number(123)  # type: ignore[arg-type]


# ---------------------------------------------------------------- unit normalization


def test_normalize_yuan() -> None:
    assert normalize_value_cny(Decimal("123"), "yuan") == Decimal("123")


def test_normalize_thousand_yuan() -> None:
    assert normalize_value_cny(Decimal("123"), "thousand_yuan") == Decimal("123000")


def test_normalize_ten_thousand_yuan() -> None:
    assert normalize_value_cny(Decimal("1.5"), "ten_thousand_yuan") == Decimal("15000")


def test_normalize_hundred_million_yuan() -> None:
    assert normalize_value_cny(Decimal("862"), "hundred_million_yuan") == Decimal("86200000000")


def test_normalize_large_decimal() -> None:
    result = normalize_value_cny(Decimal("999999999999.99"), "hundred_million_yuan")
    assert result == Decimal("99999999999999000000.00")
    assert isinstance(result, Decimal)


def test_normalize_negative() -> None:
    assert normalize_value_cny(Decimal("-123.45"), "ten_thousand_yuan") == Decimal("-1234500")


def test_normalize_preserves_exactness() -> None:
    result = normalize_value_cny(Decimal("0.1"), "ten_thousand_yuan")
    assert str(result) == "1000.0"
    assert result == Decimal("1000")


def test_normalize_rejects_non_decimal() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        normalize_value_cny(123, "yuan")  # type: ignore[arg-type]


def test_normalize_rejects_unknown_unit() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        normalize_value_cny(Decimal("1"), "dollar")


# ---------------------------------------------------------------- tokenizer
# （Gate 0 A：find_financial_number_tokens，与 parse 同一 grammar，exact token）


def test_tokens_extract_exact_number_in_cjk_context() -> None:
    assert _tokens("收入1000万元") == [("1000", 2, 6)]


def test_tokens_no_partial_substring_of_longer_token() -> None:
    # "100" / "000" 只是 "1000" 的子串 → 不是 token。
    assert _tokens("收入1000万元") == [("1000", 2, 6)]


def test_tokens_sign_belongs_to_token() -> None:
    assert _tokens("亏损-123.45万元") == [("-123.45", 2, 9)]


def test_tokens_sign_stripped_is_not_a_token() -> None:
    # "123.45" 剥掉负号 → 不是完整 token。
    assert _tokens("亏损-123.45万元") == [("-123.45", 2, 9)]


def test_tokens_plus_sign_belongs_to_token() -> None:
    assert _tokens("增长+123.45万元") == [("+123.45", 2, 9)]


def test_tokens_parentheses_belong_to_token() -> None:
    assert _tokens("(123.45)") == [("(123.45)", 0, 8)]


def test_tokens_parenthesis_stripped_is_not_a_token() -> None:
    # "123.45" 剥掉括号 → 不是完整 token。
    assert _tokens("净亏损(123.45)万元") == [("(123.45)", 3, 11)]


def test_tokens_duplicate_complete_number_is_two_tokens() -> None:
    assert _tokens("营业收入100万元，调整后100万元") == [
        ("100", 4, 7),
        ("100", 13, 16),
    ]


def test_tokens_thousands_separator_single_token() -> None:
    assert _tokens("营业收入123,456万元") == [("123,456", 4, 11)]


def test_tokens_malformed_thousands_yields_nothing() -> None:
    # "1,23" / "1,2345" malformed → 不产生 token（"1" 不是 partial token）。
    assert _tokens("收入1,23万元") == []
    assert _tokens("收入1,2345万元") == []


def test_tokens_malformed_double_decimal_yields_nothing() -> None:
    assert _tokens("收入12.3.4万元") == []


def test_tokens_plain_long_run_is_single_token() -> None:
    assert _tokens("收入1234万元") == [("1234", 2, 6)]


def test_tokens_scientific_notation_yields_nothing() -> None:
    # "1e3" 是科学计数，"1" 不是完整 token。
    assert _tokens("收入1e3万元") == []


def test_tokens_units_percent_are_outside_token() -> None:
    assert _tokens("收入100亿万元") == [("100", 2, 5)]
    assert _tokens("毛利率12%") == [("12", 3, 5)]


def test_tokens_decimal_and_comma_separator() -> None:
    assert _tokens("100, 200") == [("100", 0, 3), ("200", 5, 8)]


def test_tokens_empty() -> None:
    assert _tokens("") == []
    assert _tokens("   ") == []
    assert _tokens("营业收入约") == []


def test_tokens_rejects_non_string() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        find_financial_number_tokens(123)  # type: ignore[arg-type]


def test_every_token_round_trips_through_parse() -> None:
    """token.text 必须能被 parse_financial_number 解析（同一 grammar 保证）。"""
    text = "收入-1,234.56万元，亏损(123.45)万元，+8亿元，100.5千元"
    for token in find_financial_number_tokens(text):
        value = parse_financial_number(token.text)
        assert isinstance(value, Decimal)
    assert parse_financial_number("-1,234.56") == Decimal("-1234.56")
    assert parse_financial_number("(123.45)") == Decimal("-123.45")


def test_parse_still_rejects_partial_tokens() -> None:
    # 与 tokenizer 对齐：partial / 上下文文本都不能 parse。
    with pytest.raises(FinancialMetricValueNotNumeric):
        parse_financial_number("收入1000万元")


# ---------------------------------------------------------------- storage bounds
# （Gate 0 B：validate_financial_decimal_storage，NUMERIC(38,12) contract）


def test_storage_12_fraction_digits_ok() -> None:
    validate_financial_decimal_storage(Decimal("12345678901234.123456789012"))
    validate_financial_decimal_storage(Decimal("0.000000000001"))


def test_storage_13_fraction_digits_rejected() -> None:
    with pytest.raises(FinancialMetricStorageRangeError):
        validate_financial_decimal_storage(Decimal("0.0000000000001"))


def test_storage_max_integer_boundary_ok() -> None:
    # 10^26 - 1（26 个 9）：整数部分最大合法边界。
    validate_financial_decimal_storage(Decimal("99999999999999999999999999"))


def test_storage_ten_to_26_rejected() -> None:
    with pytest.raises(FinancialMetricStorageRangeError):
        validate_financial_decimal_storage(Decimal(10**26))


def test_storage_negative_same_rule() -> None:
    validate_financial_decimal_storage(Decimal("-99999999999999999999999999.123456789012"))
    with pytest.raises(FinancialMetricStorageRangeError):
        validate_financial_decimal_storage(Decimal(-(10**26)))
    with pytest.raises(FinancialMetricStorageRangeError):
        validate_financial_decimal_storage(Decimal("-0.0000000000001"))


def test_storage_zero_and_integers_ok() -> None:
    validate_financial_decimal_storage(Decimal("0"))
    validate_financial_decimal_storage(Decimal("1"))
    validate_financial_decimal_storage(Decimal("-1"))


def test_storage_rejects_non_decimal() -> None:
    with pytest.raises(FinancialMetricValueNotNumeric):
        validate_financial_decimal_storage(123)  # type: ignore[arg-type]
