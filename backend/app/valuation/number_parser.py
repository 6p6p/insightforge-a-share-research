"""Deterministic valuation number parser (stage 4C.2A).

估值倍数数值与 Financial 共用**同一 numeric grammar**：审计确认
`app/financial/number_parser.py` 的 token 扫描是领域无关的纯 helper
（`find_financial_number_tokens` 精确扫描完整数字 token；
`fits_numeric_38_12` 纯判定），故**直接复用**其 grammar，不在 valuation 里
重复实现扫描器。

- `parse_valuation_number(source_value_text)`：source_value_text.strip() 必须
  正好等于 quote 中一个完整 token，并转换为 Decimal；
- `validate_valuation_decimal_storage(value)`：NUMERIC(38,12) 无失真存储校验
  （复用 fits_numeric_38_12，只把金融专属错误换成 valuation 专属错误）。

v1 语法（与 Financial 完全一致）：`123` / `1,234` / `123.45` / `-123.45` /
`+123.45` / `(123.45)`。拒绝科学计数 / 百分号 / 中文数字 / 约 / 亿 / 万。
**全程 Decimal，不使用 float**；任何不匹配 → ValuationValueNotNumeric。
"""

from decimal import Decimal

from app.financial.number_parser import find_financial_number_tokens, fits_numeric_38_12
from app.valuation.errors import (
    ValuationStorageRangeError,
    ValuationValueNotNumeric,
)


def parse_valuation_number(source_value_text: str) -> Decimal:
    """把 source_value_text 解析为精确 Decimal（无 float）。

    - 复用 Financial 同一 grammar 扫描：source_value_text.strip() 必须正好等于
      一个完整 token（千分位 / 小数 / 正负号 / 括号负数 → 精确十进制）；
    - 任何不匹配（科学计数 / 百分号 / 中文数字 / 约 / 亿 / 万 / 空 / 非 str）
      → ValuationValueNotNumeric。
    """
    if not isinstance(source_value_text, str):
        raise ValuationValueNotNumeric("source_value_text 必须是 str")
    stripped = source_value_text.strip()
    if not stripped:
        raise ValuationValueNotNumeric("source_value_text 不能为空")

    tokens = find_financial_number_tokens(source_value_text)
    if len(tokens) != 1 or tokens[0].text != stripped:
        raise ValuationValueNotNumeric(
            "source_value_text 不符合 v1 估值数字语法（拒绝科学计数/百分号/中文数字/约/亿/万）"
        )
    raw = tokens[0].text
    negative = raw.startswith("(") or raw.startswith("-")
    digits = raw.lstrip("-+(")
    if digits.endswith(")"):
        digits = digits[:-1]
    digits = digits.replace(",", "")
    if "." in digits:
        int_digits, frac_digits = digits.split(".", 1)
    else:
        int_digits, frac_digits = digits, ""
    # 整数部分 + 小数位拼接成无小数点数字，再 scaleb(-len(frac)) 精确还原
    # （"123.45" → "12345" × 10^-2 = 123.45，Decimal 精确、无 float）。
    value = Decimal(int_digits + frac_digits)
    if frac_digits:
        value = value.scaleb(-len(frac_digits))
    if negative:
        value = -value
    return value


def validate_valuation_decimal_storage(value: Decimal) -> None:
    """校验 value 能无失真存入 NUMERIC(38,12)。

    - 小数位 <= 12（Decimal.as_tuple().exponent >= -12）；
    - abs(value) < 10^26（NUMERIC(38,12) 整数部分最多 26 位）。

    不满足 → ValuationStorageRangeError。**禁止静默 quantize / round /
    truncate**：数值失真必须在应用层显式拒绝，不能让 PG 自动 rounding / overflow。
    """
    if not isinstance(value, Decimal):
        raise ValuationValueNotNumeric("value 必须是 Decimal")
    if not fits_numeric_38_12(value):
        raise ValuationStorageRangeError(
            "value 超出 NUMERIC(38,12) 存储范围（小数位 > 12 或 abs >= 10^26）"
        )
