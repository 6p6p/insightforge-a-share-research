"""Deterministic annual report statement extraction provider (F1 Financial Intelligence).

真实 FinancialExtractionProvider：从年报 ParsedSourceBlock（行级文本）
**确定性**提取财务报表指标（**0 LLM / 0 数字生成 / 0 编造**）：

1. 按页检测单位标注（"单位：万元" 等）→ 该页科目行的 raw_unit；
2. 标准科目标签匹配（利润表 / 现金流量表 / 资产负债表科目 → MetricCode +
   StatementScope；长标签优先，排除摘要/附注/程序性行）；
3. 行内数字 tokens（复用 `find_financial_number_tokens` grammar）→
   本期（第 1 个）/ 上期（第 2 个）各生成一条观测候选，value span 精确定位；
4. quote = 科目行整行文本（block 切片），period 由报告期推导（annual：
   income/cash-flow → [01-01, 12-31]；balance → [NULL, 12-31]；上期 = 上年
   同期窗口）。

输出经 P3 `FinancialExtractionService` 的 numeric provenance 校验（quote
逐字 + token 精确定位）后才可落库。
"""

from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.financial.contracts import MetricCode, RawUnit, StatementScope
from app.financial.extraction.contracts import (
    ExtractedFinancialObservation,
    FinancialExtractionRequest,
)
from app.financial.number_parser import find_financial_number_tokens

FINANCIAL_EXTRACTION_PROVIDER_KEY = "statement_line_extractor"
FINANCIAL_EXTRACTION_PROVIDER_VERSION = 1

# 标准科目标签 → (metric_code, statement_scope)。顺序 = 匹配优先级（长标签
# 在前，避免 "净利润" 抢先匹配 "归属于上市公司股东的净利润"）。
_METRIC_PATTERNS: list[tuple[str, MetricCode, StatementScope]] = [
    (
        "扣除非经常性损益后的归属于上市公司股东的净利润",
        MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING,
        StatementScope.CONSOLIDATED,
    ),
    (
        # 年报常见变体（"归属于上市公司股东的扣除非经常性损益后的净利润"）。
        "归属于上市公司股东的扣除非经常性损益后的净利润",
        MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING,
        StatementScope.CONSOLIDATED,
    ),
    (
        # 宁德时代 2025 年报实测变体：跨行拆分为
        # "归属于上市公司股东" / "的扣除非经常性损益 64,507,864 ..." /
        # "的净利润"（无"后"字）。
        "归属于上市公司股东的扣除非经常性损益的净利润",
        MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING,
        StatementScope.CONSOLIDATED,
    ),
    ("归属于上市公司股东的净利润", MetricCode.NET_PROFIT_PARENT, StatementScope.CONSOLIDATED),
    ("归属于母公司所有者的净利润", MetricCode.NET_PROFIT_PARENT, StatementScope.CONSOLIDATED),
    ("归属于母公司股东的净利润", MetricCode.NET_PROFIT_PARENT, StatementScope.CONSOLIDATED),
    # 银行变体（P7 行业差异）：招商银行等上市银行用"归属于本行股东的净利润"。
    ("归属于本行股东的净利润", MetricCode.NET_PROFIT_PARENT, StatementScope.CONSOLIDATED),
    (
        "扣除非经常性损益后归属于本行股东的净利润",
        MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING,
        StatementScope.CONSOLIDATED,
    ),
    (
        "经营活动产生的现金流量净额",
        MetricCode.OPERATING_CASH_FLOW_NET,
        StatementScope.CONSOLIDATED,
    ),
    ("营业总收入", MetricCode.REVENUE, StatementScope.CONSOLIDATED),
    ("营业收入", MetricCode.REVENUE, StatementScope.CONSOLIDATED),
    ("营业成本", MetricCode.OPERATING_COST, StatementScope.CONSOLIDATED),
    ("营业利润", MetricCode.OPERATING_PROFIT, StatementScope.CONSOLIDATED),
    ("利润总额", MetricCode.PROFIT_BEFORE_TAX, StatementScope.CONSOLIDATED),
    ("净利润", MetricCode.NET_PROFIT, StatementScope.CONSOLIDATED),
    ("资产总计", MetricCode.TOTAL_ASSETS, StatementScope.CONSOLIDATED),
    ("负债合计", MetricCode.TOTAL_LIABILITIES, StatementScope.CONSOLIDATED),
    ("归属于上市公司股东的权益", MetricCode.EQUITY_PARENT, StatementScope.CONSOLIDATED),
    ("归属于母公司所有者权益合计", MetricCode.EQUITY_PARENT, StatementScope.CONSOLIDATED),
    # 常见变体（贵州茅台等）："归属于上市公司股东的净资产"。
    ("归属于上市公司股东的净资产", MetricCode.EQUITY_PARENT, StatementScope.CONSOLIDATED),
    # 银行变体（P7 行业差异）：招商银行等上市银行资产负债表用
    # "归属于本行股东权益"（主要指标摘要行）。
    ("归属于本行股东权益", MetricCode.EQUITY_PARENT, StatementScope.CONSOLIDATED),
]

# 排除行（程序性 / 摘要 / 附注）：命中任一子串 → 不提取。
_EXCLUDE_TERMS = (
    "摘要",
    "附注",
    "注：",
    "注:",
    "小计",
    "其中：",
    "其中:",
    "续",
    "单位：",
    "单位:",
    "上年同期",
    "年初余额",
    "期末余额",
    "上年年末余额",
)

# 单位检测：行文本子串 → RawUnit（长词优先）。
_UNIT_PATTERNS: tuple[tuple[str, RawUnit], ...] = (
    ("亿元", RawUnit.HUNDRED_MILLION_YUAN),
    ("万元", RawUnit.TEN_THOUSAND_YUAN),
    ("千元", RawUnit.THOUSAND_YUAN),
    ("元", RawUnit.YUAN),
)


def _non_percent_tokens(text: str) -> list:
    """行内完整数字 tokens，跳过百分比 token（token 结束处紧跟 '%'）。"""
    from app.financial.number_parser import FinancialNumberToken

    result: list[FinancialNumberToken] = []
    for token in find_financial_number_tokens(text):
        after = text[token.end : token.end + 1]
        if after == "%":
            continue
        result.append(token)
    return result


def _plausible_value(value_text: str) -> bool:
    """值合理性过滤（确定性，宁缺毋滥）：

    - 4 位年份形态（2020-2029）拒绝（"2025 年"等表头/正文年份）；
    - 金额绝对值 < 1000 拒绝（科目行金额在"元"语义下通常 ≥ 千元级；
      防止 '1'/'2'/'50' 等序号/小值噪声）。
    """
    if value_text.isdigit() and 2000 <= int(value_text) <= 2029:
        return False
    from app.financial.number_parser import parse_financial_number

    try:
        value = parse_financial_number(value_text)
    except Exception:  # noqa: BLE001 - 无法解析 → 拒绝
        return False
    return abs(value) >= 1000


def _detect_unit(text: str) -> RawUnit:
    """确定性单位检测（行文本含 "单位：万元" 等 → 对应 RawUnit；默认元）。"""
    for keyword, unit in _UNIT_PATTERNS:
        if keyword in text:
            return unit
    return RawUnit.YUAN


def _inline_unit(text: str) -> RawUnit | None:
    """行内单位标注（"归属于上市公司股东的净利润（千元）"）→ RawUnit。"""
    for keyword, unit in _UNIT_PATTERNS:
        if f"（{keyword}）" in text or f"({keyword})" in text:
            return unit
    return None


# 财务报表表头前缀（科目行合法行首；正文叙述句不匹配）。
_STATEMENT_PREFIXES = (
    "一、",
    "二、",
    "三、",
    "四、",
    "五、",
    "（一）",
    "（二）",
    "（三）",
    "（四）",
    "其中：",
    "其中:",
    "减：",
    "减:",
    "加：",
    "加:",
)


def _is_fragment_line(text: str) -> bool:
    """行是否为科目标签碎片（含标签头/尾子串且**无数字 token**）。

    真实年报表格列宽不足时，科目行被拆成多行：
    "归属于上市公司股东" / 数字行 / "的净利润"。
    """
    if find_financial_number_tokens(text):
        return False
    for label, _metric, _scope in _METRIC_PATTERNS:
        if label[:8] in text or label[-4:] in text:
            return True
    return False


def _fragment_metric(head_text: str, tail_text: str) -> tuple[MetricCode, StatementScope] | None:
    """标签碎片（头 + 尾）组合 → (metric, scope)。

    合并 = 去空白后 head+tail 应包含完整标签（如 "归属于上市公司股东" +
    "的净利润" = "归属于上市公司股东的净利润"）。
    """
    combined = (head_text + tail_text).replace(" ", "").replace("\u3000", "")
    for label, metric, scope in _METRIC_PATTERNS:
        if label in combined:
            return metric, scope
    return None


def _strip_number_tokens(text: str) -> str:
    """去掉文本中的全部数字 token（保留文字片段；跨行重构用）。"""
    result = text
    for token in find_financial_number_tokens(text):
        result = result.replace(token.text, "")
    return result


def _fragment_metric3(
    head_text: str, middle_text: str, tail_text: str
) -> tuple[MetricCode, StatementScope] | None:
    """三段式标签碎片（头 + 中间带数字 + 尾）组合 → (metric, scope)。

    宁德时代 2025 年报实测：科目行拆为
    "归属于上市公司股东" / "的扣除非经常性损益 64,507,864 ..." /
    "的净利润"——数字夹在中间碎片。合并 = head + 中间文字 + tail。
    """
    middle = _strip_number_tokens(middle_text)
    combined = (head_text + middle + tail_text).replace(" ", "").replace("\u3000", "")
    for label, metric, scope in _METRIC_PATTERNS:
        if label in combined:
            return metric, scope
    return None


def _match_metric(line_text: str) -> tuple[MetricCode, StatementScope] | None:
    """标准科目标签匹配（长标签优先；噪声行拒绝）。

    拒绝规则（真实年报实测噪声）：
    - 排除行（摘要/附注/单位/上年同期等）；
    - **行首数字**：目录/章节编号行（"1、营业收入及营业成本构成分析"）；
    - **标签必须在行首**（允许表头前缀："一、""其中：""减：" 等）——正文
      叙述句（"对净利润 50%实施现金分红…"、"2025 年，公司经营活动产生的
      现金流量净额 362 亿元…"）不匹配；
    - **百分比行不做整行拒绝**——科目主表行常带同比百分比列
      （"营业收入（千元）362,012,554 400,917,045 -9.70% …"），百分比由
      token 级过滤跳过（见 _non_percent_tokens）。
    """
    if any(term in line_text for term in _EXCLUDE_TERMS):
        return None
    stripped = line_text.strip()
    if not stripped or stripped[:1].isdigit():
        return None
    for label, metric, scope in _METRIC_PATTERNS:
        if stripped.startswith(label):
            return metric, scope
        for prefix in _STATEMENT_PREFIXES:
            if stripped.startswith(prefix + label):
                return metric, scope
    return None


def _period_for(
    metric_code: MetricCode,
    reporting_period_end: date,
    *,
    prior: bool = False,
) -> tuple[date | None, date]:
    """报告期推导（annual/semiannual/quarterly 通用）。

    - 本期 = reporting_period_end（年报 12-31 / 半年报 06-30 / 一季报 03-31）；
    - prior（报表第二列 = 上年同期）→ 上年同期末；
    - balance → period_start=None（instant）；income/cash-flow → 当期年初
      （duration 近似；期间匹配以 period_end 为准）。
    """
    end = (
        date(
            reporting_period_end.year - 1,
            reporting_period_end.month,
            reporting_period_end.day,
        )
        if prior
        else reporting_period_end
    )
    from app.financial.contracts import expected_period_kind

    if expected_period_kind(metric_code).value == "instant":
        return None, end
    return date(end.year, 1, 1), end


class StatementLineExtractionProvider:
    """确定性年报科目行提取器（真实 FinancialExtractionProvider）。"""

    provider_key = FINANCIAL_EXTRACTION_PROVIDER_KEY

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def extract(
        self, request: FinancialExtractionRequest
    ) -> list[ExtractedFinancialObservation]:
        """从 parsed blocks 提取观测候选（0 LLM；数字全部来自行文本 token）。"""
        blocks = await self._load_blocks(request.parsed_source_id)
        if not blocks:
            return []
        # 页级单位标注（"单位：万元" 行）→ 该页默认单位。
        page_units: dict[int, RawUnit] = {}
        for block in blocks:
            page = _page_of(block)
            if page is not None and page not in page_units:
                text = block.text or ""
                if "单位" in text:
                    page_units[page] = _detect_unit(text)

        observations: list[ExtractedFinancialObservation] = []
        for index, block in enumerate(blocks):
            text = (block.text or "").strip()
            if not text:
                continue
            prev_text = (blocks[index - 1].text or "").strip() if index > 0 else ""
            next_text = (blocks[index + 1].text or "").strip() if index + 1 < len(blocks) else ""
            # 跨行重构（PDF 表格列宽不足，科目行拆成多行）：
            #  模式 1：head + numbers + tail（"归属于上市公司股东" / 数字行 /
            #          "的净利润"）；
            #  模式 2：head + (tail+numbers)（"归属于上市公司股东" /
            #          "的扣除非经常性损益后的净利润 64,507,864 ..."）。
            if prev_text and _is_fragment_line(prev_text) and _non_percent_tokens(text):
                matched = _fragment_metric(prev_text, text)
                if matched is not None:
                    metric_code, scope = matched
                    observations.extend(
                        self._make_observations(
                            request, block, text, page_units, metric_code, scope
                        )
                    )
                    continue
            if (
                text[:1].isdigit()
                and index > 0
                and index + 1 < len(blocks)
                and (_is_fragment_line(prev_text) or _is_fragment_line(next_text))
            ):
                matched = _fragment_metric(prev_text, next_text)
                if matched is not None:
                    metric_code, scope = matched
                    observations.extend(
                        self._make_observations(
                            request,
                            blocks[index],
                            text,
                            page_units,
                            metric_code,
                            scope,
                        )
                    )
                    continue
            # 模式 3：三段式（head / 中间含数字 / tail 都是碎片块）——
            # "归属于上市公司股东" / "的扣除非经常性损益 64,507,864 ..." /
            # "的净利润"（宁德时代 2025 年报实测）。
            if (
                prev_text
                and next_text
                and _is_fragment_line(prev_text)
                and _is_fragment_line(next_text)
                and _non_percent_tokens(text)
                and not text[:1].isdigit()
            ):
                matched = _fragment_metric3(prev_text, text, next_text)
                if matched is not None:
                    metric_code, scope = matched
                    observations.extend(
                        self._make_observations(
                            request,
                            block,
                            text,
                            page_units,
                            metric_code,
                            scope,
                        )
                    )
                    continue
            matched = _match_metric(text)
            if matched is None:
                continue
            metric_code, scope = matched
            observations.extend(
                self._make_observations(request, block, text, page_units, metric_code, scope)
            )
        # 同 (metric, period_end, scope) 去重保留首个（主表先行；分季度/分部表
        # 的重复值不覆盖主表值）。
        return self._dedupe(observations)

    # ------------------------------------------------------------ internal

    def _make_observations(
        self,
        request,
        block,
        text: str,
        page_units: dict,
        metric_code: MetricCode,
        scope: StatementScope,
    ) -> list[ExtractedFinancialObservation]:
        """由科目行文本生成本期/上期观测候选（值合理性过滤）。"""
        tokens = _non_percent_tokens(text)
        if not tokens:
            return []
        page = _page_of(block)
        # 行内单位标注优先（"（千元）"），其次页级单位，默认元。
        unit = _inline_unit(text) or (
            page_units.get(page, RawUnit.YUAN) if page is not None else RawUnit.YUAN
        )
        made: list[ExtractedFinancialObservation] = []
        # 两列：第 1 个数字 = 本期，第 2 个 = 上期（上年同期；仅取前两个）。
        for token_index, token in enumerate(tokens[:2]):
            if not _plausible_value(token.text):
                continue
            period_start, period_end = _period_for(
                metric_code, request.reporting_period_end, prior=token_index == 1
            )
            # P7 quote 唯一化：FinancialMetricService 要求 source_value 在
            # quote_text 中是**唯一**数字 token（FinancialMetricValueAmbiguous
            # 防混淆）。真实年报行中上期值可能与其它列重复（如同比列出现
            # 相同数值）→ 将 quote 截到所选 token 结束（原文前缀，仍是逐字
            # 切片），保证该值在 quote 内唯一；未重复则保留完整行文本。
            quote_text = text
            if text.count(token.text) > 1:
                quote_text = text[: text.find(token.text) + len(token.text)]
            made.append(
                ExtractedFinancialObservation(
                    company_id=request.company_id,
                    parsed_source_id=request.parsed_source_id,
                    metric_code=metric_code,
                    statement_scope=scope,
                    period_start=period_start,
                    period_end=period_end,
                    value_text=token.text,
                    value_start=token.start,
                    value_end=token.end,
                    raw_unit=unit,
                    quote_block_id=block.block_id,
                    quote_start=_text_start(block, quote_text),
                    quote_end=_text_start(block, quote_text) + len(quote_text),
                    quote_text=quote_text,
                )
            )
        return made

    @staticmethod
    def _dedupe(
        observations: list[ExtractedFinancialObservation],
    ) -> list[ExtractedFinancialObservation]:
        """同 (metric_code, period_end, statement_scope) 保留首个（主表先行）。"""
        seen: set[tuple] = set()
        result: list[ExtractedFinancialObservation] = []
        for obs in observations:
            key = (obs.metric_code, obs.period_end, obs.statement_scope)
            if key in seen:
                continue
            seen.add(key)
            result.append(obs)
        return result

    async def _load_blocks(self, parsed_source_id: UUID) -> list:
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(ParsedSourceBlockModel)
                        .where(ParsedSourceBlockModel.parsed_source_id == parsed_source_id)
                        .order_by(ParsedSourceBlockModel.ordinal.asc())
                    )
                )
                .scalars()
                .all()
            )
        return list(rows)


def _page_of(block) -> int | None:
    locator = block.locator or {}
    return locator.get("page_number") if isinstance(locator, dict) else None


def _text_start(block, stripped_text: str) -> int:
    """stripped 文本在原始 block 文本中的起始偏移（quote 切片用原始文本）。"""
    raw = block.text or ""
    idx = raw.find(stripped_text)
    return idx if idx >= 0 else 0
