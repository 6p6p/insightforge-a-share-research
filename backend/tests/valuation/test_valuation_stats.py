"""Deterministic relative valuation stats unit tests (stage 4C.2A, spec T/U).

纯函数、无 DB：`compute_peer_median` / `compute_comparison_stats` 必须
**全 Decimal、无 float、ROUND_HALF_EVEN @ CALCULATION_SCALE=12**。覆盖：

- 奇数 peer 中位 = 排序后中间值；偶数 = 两中位算术平均（Decimal 精确，非
  statistics.median + float 混合）；
- peer_min / peer_max 与 peer_count；
- premium_discount_to_median = (target - median) / median；无限循环小数 →
  12 位确定性舍入；
- 结果不与 float 路径混合（直接断言 Decimal 精确值）；
- comparison_method 恒为 peer_median。
"""

from decimal import Decimal

from app.valuation.comparison_service import (
    CALCULATION_SCALE,
    compute_comparison_stats,
    compute_peer_median,
)

_CALCULATION_SCALE = CALCULATION_SCALE  # 12


def test_odd_peer_count_median_is_middle() -> None:
    assert compute_peer_median([Decimal("14.2"), Decimal("15.0"), Decimal("16.0")]) == Decimal(
        "15.0"
    )
    assert compute_peer_median(
        [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4"), Decimal("5")]
    ) == Decimal("3")


def test_even_peer_count_median_is_arithmetic_mean() -> None:
    median = compute_peer_median(
        [Decimal("14.2"), Decimal("15.0"), Decimal("16.0"), Decimal("17.0")]
    )
    assert median == Decimal("15.5")  # (15.0+16.0)/2


def test_even_median_three_decimal() -> None:
    median = compute_peer_median(
        [Decimal("14.10"), Decimal("14.40"), Decimal("15.33"), Decimal("16.00")]
    )
    assert median == Decimal("14.865")  # (14.40+15.33)/2，Decimal 精确


def test_median_unsorted_input_deterministic() -> None:
    """输入乱序不影响中位（确定性排序）。"""
    a = compute_peer_median([Decimal("16.0"), Decimal("14.2"), Decimal("15.0")])
    b = compute_peer_median([Decimal("14.2"), Decimal("15.0"), Decimal("16.0")])
    assert a == b == Decimal("15.0")


def test_stats_odd_peers_clean_premium() -> None:
    stats = compute_comparison_stats(
        Decimal("15.3"), [Decimal("14.2"), Decimal("15.0"), Decimal("16.0")]
    )
    assert stats.comparison_method == "peer_median"
    assert stats.peer_count == 3
    assert stats.peer_median == Decimal("15.0")
    assert stats.peer_min == Decimal("14.2")
    assert stats.peer_max == Decimal("16.0")
    assert stats.premium_discount_to_median == Decimal("0.02")  # (15.3-15.0)/15.0


def test_stats_discount_negative() -> None:
    stats = compute_comparison_stats(
        Decimal("14.0"), [Decimal("14.2"), Decimal("15.0"), Decimal("16.0")]
    )
    assert stats.premium_discount_to_median < 0  # 相对折价
    # (14.0-15.0)/15.0 = -1/15 = -0.0666... → 12 位 ROUND_HALF_EVEN。
    assert stats.premium_discount_to_median == Decimal("-0.066666666667")


def test_stats_premium_repeating_quantized_round_half_even() -> None:
    """除法无限循环小数 → 12 位确定性舍入（精确 Decimal 断言，无 float 误差）。"""
    stats = compute_comparison_stats(
        Decimal("15.3"), [Decimal("14.2"), Decimal("15.0"), Decimal("16.0"), Decimal("17.0")]
    )
    assert stats.peer_median == Decimal("15.5")
    # -0.2/15.5 = -0.0129032258064516... → 13 位 4 → 向下舍 → -0.012903225806。
    assert stats.premium_discount_to_median == Decimal("-0.012903225806")


def test_stats_all_results_within_12_decimal_places() -> None:
    """派生结果全部满足 NUMERIC(38,12)：小数位 <= 12（可直接落库无失真）。"""
    stats = compute_comparison_stats(
        Decimal("15.3"), [Decimal("14.2"), Decimal("15.0"), Decimal("16.0"), Decimal("17.0")]
    )
    for value in (
        stats.peer_median,
        stats.peer_min,
        stats.peer_max,
        stats.premium_discount_to_median,
    ):
        assert value.as_tuple().exponent >= -12


def test_stats_scale_constant_is_twelve() -> None:
    """CALCULATION_SCALE 冻结为 12（NUMERIC(38,12) 契约）。"""
    assert _CALCULATION_SCALE == 12
