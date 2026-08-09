"""Deterministic financial calculation formulas unit tests (stage 4B.2B, spec M).

零 LLM / 零 Chroma / 零 DB：验证 7 个 v1 公式的 Decimal 精确性、除法
quantize 到 CALCULATION_SCALE=12 / ROUND_HALF_EVEN、baseline / 分母必须 > 0，
以及 ratio 结果存小数形式（0.1234）而非 12.34。
"""

from decimal import Decimal

import pytest

from app.financial.calculations.contracts import (
    CalculationCode,
    CalculationResultUnit,
    InputRole,
)
from app.financial.calculations.errors import (
    FinancialCalculationGrowthBaseNotPositive,
    FinancialCalculationZeroDenominator,
)
from app.financial.calculations.formulas import (
    CALCULATION_SCALE,
    absolute_change_cny,
    compute_calculation_result,
    debt_to_assets_ratio,
    gross_margin,
    growth_rate,
    net_margin_parent,
    operating_margin,
)


def test_calculation_scale_is_12() -> None:
    assert CALCULATION_SCALE == 12


# ---------------------------------------------------------------- absolute change


def test_absolute_change_exact_subtraction() -> None:
    result = absolute_change_cny(Decimal("20000000000"), Decimal("10000000000"))
    assert result == Decimal("10000000000")
    assert isinstance(result, Decimal)


def test_absolute_change_preserves_decimal_places() -> None:
    result = absolute_change_cny(Decimal("123.45"), Decimal("100.1"))
    assert result == Decimal("23.35")


def test_absolute_change_negative_result() -> None:
    assert absolute_change_cny(Decimal("50"), Decimal("100")) == Decimal("-50")


def test_absolute_change_no_float_drift() -> None:
    result = absolute_change_cny(Decimal("0.3"), Decimal("0.1"))
    assert str(result) == "0.2"


# ---------------------------------------------------------------- growth rate


def test_growth_rate_quantizes_to_12_places() -> None:
    result = growth_rate(Decimal("4"), Decimal("3"))
    assert result == Decimal("0.333333333333")  # (4-3)/3 = 1/3，quantize 到 12 位


def test_growth_rate_ratio_form_not_percent() -> None:
    # (current-baseline)/baseline 存 0.2，不存 20。
    result = growth_rate(Decimal("120"), Decimal("100"))
    assert result == Decimal("0.200000000000")
    assert result == Decimal("0.2")


def test_growth_rate_negative_growth() -> None:
    assert growth_rate(Decimal("80"), Decimal("100")) == Decimal("-0.200000000000")


def test_growth_rate_round_half_even_down() -> None:
    # 13 位后恰为 5，且 12 位为偶数 0 → 舍入到偶数（向下）。
    result = growth_rate(Decimal("2.0000000000005"), Decimal("1"))
    assert result == Decimal("1.000000000000")


def test_growth_rate_round_half_even_up() -> None:
    # 13 位后恰为 5，且 12 位为奇数 1 → 舍入到偶数（向上）。
    result = growth_rate(Decimal("2.0000000000015"), Decimal("1"))
    assert result == Decimal("1.000000000002")


def test_growth_rate_baseline_zero_rejected() -> None:
    with pytest.raises(FinancialCalculationGrowthBaseNotPositive):
        growth_rate(Decimal("100"), Decimal("0"))


def test_growth_rate_baseline_negative_rejected() -> None:
    with pytest.raises(FinancialCalculationGrowthBaseNotPositive):
        growth_rate(Decimal("100"), Decimal("-50"))


# ---------------------------------------------------------------- ratios


def test_gross_margin() -> None:
    result = gross_margin(Decimal("100"), Decimal("60"))
    assert result == Decimal("0.400000000000")
    assert result == Decimal("0.4")


def test_gross_margin_negative_margin_allowed() -> None:
    # 成本 > 收入 → 负毛利率（合法）。
    assert gross_margin(Decimal("100"), Decimal("150")) == Decimal("-0.500000000000")


def test_operating_margin() -> None:
    assert operating_margin(Decimal("100"), Decimal("20")) == Decimal("0.200000000000")


def test_net_margin_parent() -> Decimal:
    assert net_margin_parent(Decimal("100"), Decimal("15")) == Decimal("0.150000000000")


def test_debt_to_assets_ratio() -> None:
    assert debt_to_assets_ratio(Decimal("200"), Decimal("80")) == Decimal("0.400000000000")


def test_ratio_zero_denominator_rejected() -> None:
    with pytest.raises(FinancialCalculationZeroDenominator):
        gross_margin(Decimal("0"), Decimal("60"))
    with pytest.raises(FinancialCalculationZeroDenominator):
        operating_margin(Decimal("0"), Decimal("20"))
    with pytest.raises(FinancialCalculationZeroDenominator):
        net_margin_parent(Decimal("0"), Decimal("15"))
    with pytest.raises(FinancialCalculationZeroDenominator):
        debt_to_assets_ratio(Decimal("0"), Decimal("80"))


def test_ratio_negative_denominator_rejected() -> None:
    with pytest.raises(FinancialCalculationZeroDenominator):
        gross_margin(Decimal("-100"), Decimal("60"))


# ---------------------------------------------------------------- dispatcher


def _values(**kwargs) -> dict:
    return {InputRole(k): v for k, v in kwargs.items()}


def test_dispatch_absolute_change_returns_cny_unit() -> None:
    value, unit = compute_calculation_result(
        CalculationCode.ABSOLUTE_CHANGE_CNY,
        _values(current=Decimal("200"), baseline=Decimal("150")),
    )
    assert value == Decimal("50")
    assert unit == CalculationResultUnit.CNY


def test_dispatch_yoy_ratio_form() -> None:
    value, unit = compute_calculation_result(
        CalculationCode.YOY_GROWTH_RATE,
        _values(current=Decimal("120"), baseline=Decimal("100")),
    )
    assert value == Decimal("0.2")
    assert unit == CalculationResultUnit.RATIO


def test_dispatch_qoq_same_formula() -> None:
    value, unit = compute_calculation_result(
        CalculationCode.QOQ_GROWTH_RATE,
        _values(current=Decimal("110"), baseline=Decimal("100")),
    )
    assert value == Decimal("0.1")
    assert unit == CalculationResultUnit.RATIO


def test_dispatch_margins_ratio_unit() -> None:
    for code, args in (
        (
            CalculationCode.GROSS_MARGIN,
            _values(revenue=Decimal("100"), operating_cost=Decimal("60")),
        ),
        (
            CalculationCode.OPERATING_MARGIN,
            _values(revenue=Decimal("100"), operating_profit=Decimal("20")),
        ),
        (
            CalculationCode.NET_MARGIN_PARENT,
            _values(revenue=Decimal("100"), net_profit_parent=Decimal("15")),
        ),
        (
            CalculationCode.DEBT_TO_ASSETS_RATIO,
            _values(total_assets=Decimal("200"), total_liabilities=Decimal("80")),
        ),
    ):
        value, unit = compute_calculation_result(code, args)
        assert unit == CalculationResultUnit.RATIO
        assert isinstance(value, Decimal)
