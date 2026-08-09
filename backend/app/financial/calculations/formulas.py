"""Deterministic financial calculation formulas (stage 4B.2B).

v1 公式（全部 Decimal，禁止 float）：
- `absolute_change_cny(current, baseline)`：current - baseline（减法**精确**，
  输入已是 NUMERIC(38,12)，结果小数位 <= 12，无需 quantize）；
- `yoy_growth_rate` / `qoq_growth_rate`：growth_rate(current, baseline) =
  (current - baseline) / baseline，**baseline 必须 > 0**（否则
  FinancialCalculationGrowthBaseNotPositive）；
- `gross_margin` = (revenue - operating_cost) / revenue；
  `operating_margin` = operating_profit / revenue；
  `net_margin_parent` = net_profit_parent / revenue；
  `debt_to_assets_ratio` = total_liabilities / total_assets。
  四个 ratio 的分母必须 > 0（否则 FinancialCalculationZeroDenominator）。

除法统一 quantize 到 `CALCULATION_SCALE = 12` 位小数、`ROUND_HALF_EVEN`
（`quantize("0.000000000001")`）；result 超 NUMERIC(38,12) 范围由 Service
显式拒绝（FinancialCalculationStorageRangeError），不在此静默处理。

ratio 结果存小数形式（0.1234），**不存 12.34**（result_unit = ratio）。
"""

from collections.abc import Mapping
from decimal import ROUND_HALF_EVEN, Decimal

from app.financial.calculations.contracts import (
    CalculationCode,
    CalculationResultUnit,
    InputRole,
)
from app.financial.calculations.errors import (
    FinancialCalculationGrowthBaseNotPositive,
    FinancialCalculationInputError,
    FinancialCalculationZeroDenominator,
)

# 除法结果固定保留 12 位小数、银行家舍入。
CALCULATION_SCALE = 12
_QUANTUM = Decimal("0.000000000001")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_QUANTUM, rounding=ROUND_HALF_EVEN)


def absolute_change_cny(current: Decimal, baseline: Decimal) -> Decimal:
    """current - baseline（精确减法；输入为 NUMERIC(38,12) 值，结果不超 12 位小数）。"""
    return current - baseline


def growth_rate(current: Decimal, baseline: Decimal) -> Decimal:
    """(current - baseline) / baseline；baseline 必须 > 0，结果 quantize 到 12 位。"""
    if baseline <= 0:
        raise FinancialCalculationGrowthBaseNotPositive("baseline 必须 > 0")
    return _quantize((current - baseline) / baseline)


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal:
    """numerator / denominator；分母必须 > 0，结果 quantize 到 12 位。"""
    if denominator <= 0:
        raise FinancialCalculationZeroDenominator("分母必须 > 0")
    return _quantize(numerator / denominator)


def gross_margin(revenue: Decimal, operating_cost: Decimal) -> Decimal:
    """毛利率 = (revenue - operating_cost) / revenue。"""
    return _ratio(revenue - operating_cost, revenue)


def operating_margin(revenue: Decimal, operating_profit: Decimal) -> Decimal:
    """营业利润率 = operating_profit / revenue。"""
    return _ratio(operating_profit, revenue)


def net_margin_parent(revenue: Decimal, net_profit_parent: Decimal) -> Decimal:
    """归母净利率 = net_profit_parent / revenue。"""
    return _ratio(net_profit_parent, revenue)


def debt_to_assets_ratio(total_assets: Decimal, total_liabilities: Decimal) -> Decimal:
    """资产负债率 = total_liabilities / total_assets。"""
    return _ratio(total_liabilities, total_assets)


def compute_calculation_result(
    calculation_code: CalculationCode,
    values: Mapping[InputRole, Decimal],
) -> tuple[Decimal, CalculationResultUnit]:
    """按 calculation_code 确定性派发公式，返回 (result_value, result_unit)。

    `values` 键必须是该 code 的 input roles 全集（调用方保证），值是已登记
    Observation 的 `normalized_value_cny`。
    """
    if calculation_code == CalculationCode.ABSOLUTE_CHANGE_CNY:
        return (
            absolute_change_cny(values[InputRole.CURRENT], values[InputRole.BASELINE]),
            CalculationResultUnit.CNY,
        )
    if calculation_code == CalculationCode.YOY_GROWTH_RATE:
        return (
            growth_rate(values[InputRole.CURRENT], values[InputRole.BASELINE]),
            CalculationResultUnit.RATIO,
        )
    if calculation_code == CalculationCode.QOQ_GROWTH_RATE:
        return (
            growth_rate(values[InputRole.CURRENT], values[InputRole.BASELINE]),
            CalculationResultUnit.RATIO,
        )
    if calculation_code == CalculationCode.GROSS_MARGIN:
        return (
            gross_margin(values[InputRole.REVENUE], values[InputRole.OPERATING_COST]),
            CalculationResultUnit.RATIO,
        )
    if calculation_code == CalculationCode.OPERATING_MARGIN:
        return (
            operating_margin(values[InputRole.REVENUE], values[InputRole.OPERATING_PROFIT]),
            CalculationResultUnit.RATIO,
        )
    if calculation_code == CalculationCode.NET_MARGIN_PARENT:
        return (
            net_margin_parent(values[InputRole.REVENUE], values[InputRole.NET_PROFIT_PARENT]),
            CalculationResultUnit.RATIO,
        )
    if calculation_code == CalculationCode.DEBT_TO_ASSETS_RATIO:
        return (
            debt_to_assets_ratio(
                values[InputRole.TOTAL_ASSETS], values[InputRole.TOTAL_LIABILITIES]
            ),
            CalculationResultUnit.RATIO,
        )
    raise FinancialCalculationInputError(f"不支持 calculation_code: {calculation_code}")
