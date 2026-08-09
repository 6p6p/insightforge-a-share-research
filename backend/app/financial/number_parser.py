"""Deterministic financial number parser + unit normalization (stage 4B.2A).

`parse_financial_number(source_value_text)` 把**原文文本**里的财务数值解析为
`Decimal`；`find_financial_number_tokens(quote_text)` 扫描引用文本里的**完整**
财务数字 token（与 parse 同一 grammar，用于 exact provenance 校验）；
`normalize_value_cny(raw_value, raw_unit)` 把原始单位确定性换算成人民币元；
`validate_financial_decimal_storage(value)` 校验 Decimal 能无失真存入
NUMERIC(38,12)。**全程 Decimal，不使用 float**——财务数值必须是精确十进制。

v1 语法（严格，前后允许普通空白）：
- 整数：`123`
- 千分位：`1,234`（`,` 只能作为 3 位分组分隔）
- 小数：`123.45`（`.` 后至少 1 位）
- 正负：`-123.45` / `+123.45`
- 括号负数：`(123.45)` → `-123.45`

拒绝（→ FinancialMetricValueNotNumeric）：科学计数（`1e3`）、百分号（`12%`）、
中文数字（`一百`）、约数（`约100`）、带单位（`100亿` / `100万元`）——单位由
`raw_unit` 单独表达，数字文本里不允许出现。

**exact provenance（Gate 0）**：`find_financial_number_tokens` 与
`parse_financial_number` 共用同一 grammar——token 必须是**完整**数字，
`"收入1000万元"` 只有 `"1000"` 一个 token（`"100"` / `"000"` 不是），
`"-123.45"` / `"(123.45)"` 的符号与括号属于 token，不得剥离。禁止 substring
partial match / fuzzy / normalize 后匹配 / 自动纠错。
"""

from dataclasses import dataclass
from decimal import Decimal

from app.financial.errors import (
    FinancialMetricStorageRangeError,
    FinancialMetricValueNotNumeric,
)

# NUMERIC(38,12) 存储边界：整数部分最多 26 位（abs < 10^26）、小数位最多 12 位。
_MAX_ABS_NUMERIC_38_12 = Decimal("1E+26")

_DIGITS = frozenset("0123456789")
# 数字 token 右侧紧贴这些字符说明数字还未结束（partial match）：数字、小数点、
# 科学计数 e/E。
_NUMBER_RIGHT_CONTINUATION = frozenset("0123456789.eE")
# 数字 token 左侧紧贴这些字符说明该位置不是独立 token 起点：数字、+/-、
# 括号、小数点、千分位逗号（"1,234" 里的 "234"、"(-123)" 里的 "123"）、
# 科学计数 e/E（"1e3" 里的 "3" 不是独立 token）。
_NUMBER_LEFT_BLOCK = frozenset("0123456789+-().,eE")
# token 只能以数字 / +/- / 左括号开头。
_NUMBER_START = frozenset("0123456789+-(")


@dataclass(frozen=True)
class FinancialNumberToken:
    """quote_text 中一个完整财务数字 token 的精确 span。

    - `text` = quote_text[start:end] 的原样切片（无前后空白）；
    - `start` / `end`：Python 字符索引，[start, end)；
    - 与 `parse_financial_number` 同一 grammar：只有完整 token 才可解析，
      "-123.45" / "(123.45)" 的符号与括号属于 token，不得剥离。
    """

    text: str
    start: int
    end: int


def _read_int_part(text: str, p: int) -> tuple[int, int] | None:
    """读整数部分（p 必须落在数字上），返回 [start, end_exclusive)。

    千分位必须成组：`1,234` 合法；`1,23` / `1,2345` 是 malformed → 整体返回
    None（禁止把 "1" 当成 token 的 partial match）。`123, `（逗号后不是数字）
    视为分隔符，数字到逗号为止。
    """
    start = p
    j = p
    while j < len(text) and text[j] in _DIGITS and (j - p) < 3:
        j += 1
    if j == p:
        return None
    if j < len(text) and text[j] in _DIGITS:
        # 无逗号的长数字串（"1234"）→ 全部消费。
        while j < len(text) and text[j] in _DIGITS:
            j += 1
        return start, j
    while j < len(text) and text[j] == ",":
        if j + 1 >= len(text) or text[j + 1] not in _DIGITS:
            # "123, "：逗号是分隔符，数字到此结束。
            break
        k = j + 1
        cnt = 0
        while k < len(text) and text[k] in _DIGITS:
            k += 1
            cnt += 1
        if cnt != 3:
            return None  # "1,23" / "1,2345"：malformed 千分位
        j = k
    return start, j


def _match_number_token(text: str, pos: int) -> tuple[int, int] | None:
    """在 pos 处尝试匹配一个完整数字 token，成功返回 [start, end)，否则 None。"""
    p = pos
    open_paren = False
    if p < len(text) and text[p] == "(":
        open_paren = True
        p += 1
    if p < len(text) and text[p] in "+-":
        if open_paren:
            return None  # "(-123)"：括号负数不能再带 +/- 符号
        p += 1
    if p >= len(text) or text[p] not in _DIGITS:
        return None  # "+" / "(" 单独出现不是数字
    int_span = _read_int_part(text, p)
    if int_span is None:
        return None
    p = int_span[1]
    if p < len(text) and text[p] == ".":
        if p + 1 >= len(text) or text[p + 1] not in _DIGITS:
            return None  # "123." / "123.x"：非完整 token
        p += 1
        while p < len(text) and text[p] in _DIGITS:
            p += 1
    if p < len(text) and text[p] == ")":
        if not open_paren:
            return None
        p += 1
    elif open_paren:
        return None
    if p < len(text):
        nxt = text[p]
        if nxt in _NUMBER_RIGHT_CONTINUATION:
            return None
        if nxt == "," and p + 1 < len(text) and text[p + 1] in _DIGITS:
            return None
    return pos, p


def find_financial_number_tokens(text: str) -> list[FinancialNumberToken]:
    """扫描 text，返回其中所有完整财务数字 token（按位置升序，deterministic）。

    与 `parse_financial_number` 同一 grammar；token 必须是**完整**数字，禁止
    substring partial match：

    - `"收入1000万元"` → 只有 `"1000"` 一个 token，"100" / "000" 都不是；
    - `"亏损-123.45万元"` → 只有 `"-123.45"`（负号属于 token），"123.45" 不是；
    - `"(123.45)"` → 只有 `"(123.45)"`（括号属于 token），"123.45" 不是；
    - 两个完整 `"100"` → 两个 token（调用方据此判 Ambiguous）。

    空 text / 无数字 → []（调用方决定 NotFound / Ambiguous）。
    """
    if not isinstance(text, str):
        raise FinancialMetricValueNotNumeric("text 必须是 str")
    tokens: list[FinancialNumberToken] = []
    n = len(text)
    pos = 0
    while pos < n:
        ch = text[pos]
        if ch in _NUMBER_START:
            if pos > 0 and text[pos - 1] in _NUMBER_LEFT_BLOCK:
                pos += 1
                continue
            span = _match_number_token(text, pos)
            if span is not None:
                start, end = span
                tokens.append(FinancialNumberToken(text=text[start:end], start=start, end=end))
                pos = end
                continue
        pos += 1
    return tokens


def parse_financial_number(source_value_text: str) -> Decimal:
    """把 source_value_text 解析为精确 Decimal（无 float）。

    - 千分位 / 小数 / 正负号 / 括号负数 → 精确十进制；
    - 任何不匹配 v1 语法的文本（科学计数 / 百分号 / 中文数字 / 约 / 亿 / 万 /
      其他非数字字符）→ FinancialMetricValueNotNumeric；
    - 空白只在首尾允许，数字中间不允许；
    - 与 `find_financial_number_tokens` 同一 grammar：source_value_text.strip()
      必须正好等于一个完整 token。
    """
    if not isinstance(source_value_text, str):
        raise FinancialMetricValueNotNumeric("source_value_text 必须是 str")
    stripped = source_value_text.strip()
    if not stripped:
        raise FinancialMetricValueNotNumeric("source_value_text 不能为空")

    tokens = find_financial_number_tokens(source_value_text)
    if len(tokens) != 1 or tokens[0].text != stripped:
        raise FinancialMetricValueNotNumeric(
            "source_value_text 不符合 v1 财务数字语法（拒绝科学计数/百分号/中文数字/约/亿/万）"
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


def fits_numeric_38_12(value: Decimal) -> bool:
    """Decimal 能否无失真存入 NUMERIC(38,12)：小数位 <= 12 且 abs < 10^26。

    纯判定，供 metric（→ FinancialMetricStorageRangeError）与 calculation
    （→ FinancialCalculationStorageRangeError）复用；不抛错。
    """
    if not isinstance(value, Decimal):
        return False
    if value.as_tuple().exponent < -12:
        return False
    return abs(value) < _MAX_ABS_NUMERIC_38_12


def validate_financial_decimal_storage(value: Decimal) -> None:
    """校验 value 能无失真存入 NUMERIC(38,12)。

    - 小数位 <= 12（Decimal.as_tuple().exponent >= -12）；
    - abs(value) < 10^26（NUMERIC(38,12) 整数部分最多 26 位）。

    不满足 → FinancialMetricStorageRangeError。**禁止静默 quantize / round /
    truncate**：数值失真必须在应用层显式拒绝，不能让 PG 自动 rounding / overflow。
    """
    if not isinstance(value, Decimal):
        raise FinancialMetricValueNotNumeric("value 必须是 Decimal")
    if not fits_numeric_38_12(value):
        raise FinancialMetricStorageRangeError(
            "value 超出 NUMERIC(38,12) 存储范围（小数位 > 12 或 abs >= 10^26）"
        )
