"""Deterministic financial number parser + unit normalization (stage 4B.2A).

`parse_financial_number(source_value_text)` 把**原文文本**里的财务数值解析为
`Decimal`；`normalize_value_cny(raw_value, raw_unit)` 把原始单位确定性换算成
人民币元。**全程 Decimal，不使用 float**——财务数值必须是精确十进制。

v1 语法（严格，前后允许普通空白）：
- 整数：`123`
- 千分位：`1,234`（`,` 只能作为 3 位分组分隔）
- 小数：`123.45`（`.` 后至少 1 位）
- 正负：`-123.45` / `+123.45`
- 括号负数：`(123.45)` → `-123.45`

拒绝（→ FinancialMetricValueNotNumeric）：科学计数（`1e3`）、百分号（`12%`）、
中文数字（`一百`）、约数（`约100`）、带单位（`100亿` / `100万元`）——单位由
`raw_unit` 单独表达，数字文本里不允许出现。
"""

import re
from decimal import Decimal

from app.financial.errors import FinancialMetricValueNotNumeric

# 数字语法：括号可选（表示负数）→ 符号可选 → 整数（千分位可选）→ 可选小数。
# 首尾只允许普通空白；`,` 只能作为 3 位分组；`.` 后至少 1 位。
_NUMBER_RE = re.compile(
    r"^[ \t\r\n\f\v]*"
    r"(?P<open>\(?)"
    r"(?P<sign>[-+]?)"
    r"(?P<int>\d{1,3}(?:,\d{3})*|\d+)"
    r"(?:\.(?P<frac>\d+))?"
    r"(?P<close>\)?)"
    r"[ \t\r\n\f\v]*$"
)


def parse_financial_number(source_value_text: str) -> Decimal:
    """把 source_value_text 解析为精确 Decimal（无 float）。

    - 千分位 / 小数 / 正负号 / 括号负数 → 精确十进制；
    - 任何不匹配 v1 语法的文本（科学计数 / 百分号 / 中文数字 / 约 / 亿 / 万 /
      其他非数字字符）→ FinancialMetricValueNotNumeric；
    - 空白只在首尾允许，数字中间不允许。
    """
    if not isinstance(source_value_text, str):
        raise FinancialMetricValueNotNumeric("source_value_text 必须是 str")
    stripped = source_value_text.strip()
    if not stripped:
        raise FinancialMetricValueNotNumeric("source_value_text 不能为空")

    match = _NUMBER_RE.match(source_value_text)
    if match is None:
        raise FinancialMetricValueNotNumeric(
            "source_value_text 不符合 v1 财务数字语法（拒绝科学计数/百分号/中文数字/约/亿/万）"
        )
    open_paren = match.group("open")
    close_paren = match.group("close")
    sign = match.group("sign")
    if (open_paren == "(") != (close_paren == ")"):
        raise FinancialMetricValueNotNumeric("括号不配对")
    if open_paren == "(" and sign:
        raise FinancialMetricValueNotNumeric("括号负数不能再带 +/- 符号")

    negative = open_paren == "(" or sign == "-"
    int_digits = match.group("int").replace(",", "")
    frac_digits = match.group("frac") or ""
    digits = int_digits + frac_digits
    value = Decimal(digits)
    if frac_digits:
        value = value.scaleb(-len(frac_digits))
    if negative:
        value = -value
    return value


# raw_unit → 人民币元换算系数（全 Decimal，v1 只支持 CNY，不泛化外币）。
_UNIT_MULTIPLIER_CNY: dict[str, Decimal] = {
    "yuan": Decimal("1"),
    "thousand_yuan": Decimal("1000"),
    "ten_thousand_yuan": Decimal("10000"),
    "hundred_million_yuan": Decimal("100000000"),
}


def normalize_value_cny(raw_value: Decimal, raw_unit: str) -> Decimal:
    """把原始单位数值确定性换算为人民币元（Decimal × 整数系数，无 float）。

    raw_unit 必须是 v1 冻结单位之一（yuan / thousand_yuan / ten_thousand_yuan /
    hundred_million_yuan）；未知单位 → KeyError（上游 draft 构造已用枚举约束，
    这里再兜底抛 FinancialMetricValueNotNumeric）。
    """
    if not isinstance(raw_value, Decimal):
        raise FinancialMetricValueNotNumeric("raw_value 必须是 Decimal")
    multiplier = _UNIT_MULTIPLIER_CNY.get(raw_unit)
    if multiplier is None:
        raise FinancialMetricValueNotNumeric(f"未知 raw_unit: {raw_unit}")
    return raw_value * multiplier
