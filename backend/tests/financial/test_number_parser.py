"""Financial number parser + unit normalization unit tests (stage 4B.2A, spec J/K).

零 LLM / 零 Chroma / 零 DB：验证 parse_financial_number 的 v1 严格语法与
Decimal 精确性，以及 normalize_value_cny 的 4 档单位换算（全程无 float）。
"""

from decimal import Decimal

import pytest

from app.financial.errors import FinancialMetricValueNotNumeric
from app.financial.number_parser import normalize_value_cny, parse_financial_number

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
