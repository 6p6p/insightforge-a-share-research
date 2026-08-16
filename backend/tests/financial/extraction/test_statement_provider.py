"""Statement line extraction provider unit tests (F1).

- 科目标签匹配（长标签优先 / 排除行）；
- 单位检测 / period 推导（duration / instant / prior）；
- provider.extract（fake sessionmaker + 内存 blocks）→ 观测候选（本期/上期、
  value span、quote 切片、unit）。
"""

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.financial.contracts import MetricCode, RawUnit, StatementScope
from app.financial.extraction.contracts import FinancialExtractionRequest
from app.financial.extraction.statement_provider import (
    StatementLineExtractionProvider,
    _detect_unit,
    _match_metric,
    _non_percent_tokens,
    _period_for,
    _plausible_value,
)

_COMPANY_ID = uuid4()
_PARSED_ID = uuid4()
_PERIOD_END = date(2024, 12, 31)


def _request(**overrides) -> FinancialExtractionRequest:
    base = dict(
        company_id=_COMPANY_ID,
        parsed_source_id=_PARSED_ID,
        reporting_period_end=_PERIOD_END,
    )
    base.update(overrides)
    return FinancialExtractionRequest(**base)


class FakeBlock:
    def __init__(self, block_id: UUID, text: str, *, page: int = 1, ordinal: int = 1) -> None:
        self.block_id = block_id
        self.text = text
        self.ordinal = ordinal
        self.locator = {"type": "pdf_page", "page_number": page, "line_index": ordinal}


class FakeSession:
    def __init__(self, blocks: list) -> None:
        self._blocks = blocks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def execute(self, stmt):
        class Rows:
            def __init__(self, rows):
                self._rows = rows

            def scalars(self):
                return self

            def all(self):
                return self._rows

        return Rows(self._blocks)


class FakeSessionMaker:
    def __init__(self, blocks: list) -> None:
        self._blocks = blocks

    def __call__(self):
        return FakeSession(self._blocks)


# ---------------------------------------------------------------- 纯函数


def test_match_metric_priority() -> None:
    assert _match_metric("归属于上市公司股东的净利润 10.00") == (
        MetricCode.NET_PROFIT_PARENT,
        StatementScope.CONSOLIDATED,
    )
    assert _match_metric("净利润 10.00") == (MetricCode.NET_PROFIT, StatementScope.CONSOLIDATED)
    assert _match_metric("营业收入 10.00") == (MetricCode.REVENUE, StatementScope.CONSOLIDATED)


def test_match_metric_excludes_procedural_lines() -> None:
    assert _match_metric("营业收入（附注五）10.00") is None
    assert _match_metric("营业收入摘要 10.00") is None
    assert _match_metric("单位：万元") is None
    assert _match_metric("本报告期营业收入比上年同期增减（%）") is None


def test_match_metric_excludes_noise_lines() -> None:
    # 目录/章节编号行（行首数字）。
    assert _match_metric("1、营业收入及营业成本构成分析") is None
    assert _match_metric("2、按国际会计准则披露的财务报告中净利润") is None
    # 标签不在行首 6 字符内（正文叙述句）。
    assert _match_metric("对净利润 50%实施现金分红，累计分红将接近千亿元") is None
    assert _match_metric("2025 年，公司经营活动产生的现金流量净额 362 亿元") is None
    assert _match_metric("研发投入占营业收入的比例 5.14% 4.58%") is None
    # 真实科目行（含同比百分比列）仍匹配——百分比由 token 级过滤。
    assert _match_metric("一、营业总收入 400,000,000,000 350,000,000,000") == (
        MetricCode.REVENUE,
        StatementScope.CONSOLIDATED,
    )
    assert _match_metric("归属于上市公司股东的净利润 8,000,000.00 7,000,000.00") == (
        MetricCode.NET_PROFIT_PARENT,
        StatementScope.CONSOLIDATED,
    )
    assert _match_metric("营业收入合计 423,701,834 100.00% 362,012,554") == (
        MetricCode.REVENUE,
        StatementScope.CONSOLIDATED,
    )


def test_detect_unit() -> None:
    assert _detect_unit("单位：万元") == RawUnit.TEN_THOUSAND_YUAN
    assert _detect_unit("单位：亿元") == RawUnit.HUNDRED_MILLION_YUAN
    assert _detect_unit("单位：千元") == RawUnit.THOUSAND_YUAN
    assert _detect_unit("单位：元") == RawUnit.YUAN
    assert _detect_unit("营业收入 10.00") == RawUnit.YUAN


def test_non_percent_tokens_skips_percent_values() -> None:
    tokens = _non_percent_tokens("营业收入（千元） 362,012,554 400,917,045 -9.70% 328,593,988")
    assert [t.text for t in tokens] == [
        "362,012,554",
        "400,917,045",
        "328,593,988",
    ]
    # 全百分比行 → 空（无金额可提取）。
    assert _non_percent_tokens("研发投入占营业收入的比例 5.14% 4.58%") == []


def test_plausible_value() -> None:
    assert _plausible_value("45,678,901.23") is True
    assert _plausible_value("1000") is True
    assert _plausible_value("999") is False  # 过小
    assert _plausible_value("1") is False
    assert _plausible_value("2025") is False  # 年份形态
    assert _plausible_value("2026") is False
    assert _plausible_value("2019") is False  # 4 位年份形态同样拒绝
    assert _plausible_value("1999") is True  # 非 2000-2029 的 4 位数（罕见但放行）


def test_period_for() -> None:
    start, end = _period_for(MetricCode.REVENUE, _PERIOD_END)
    assert (start, end) == (date(2024, 1, 1), date(2024, 12, 31))
    start, end = _period_for(MetricCode.REVENUE, _PERIOD_END, prior=True)
    assert (start, end) == (date(2023, 1, 1), date(2023, 12, 31))
    start, end = _period_for(MetricCode.TOTAL_ASSETS, _PERIOD_END)
    assert (start, end) == (None, date(2024, 12, 31))
    start, end = _period_for(MetricCode.TOTAL_ASSETS, _PERIOD_END, prior=True)
    assert (start, end) == (None, date(2023, 12, 31))


# ---------------------------------------------------------------- provider.extract


@pytest.mark.asyncio
async def test_extract_cross_line_fragment_reconstruction() -> None:
    """跨行表格（标签碎片 + 数字行 + 标签尾）→ 正确识别科目并提取。"""
    blocks = [
        FakeBlock(uuid4(), "归属于上市公司股东", page=7, ordinal=10),
        FakeBlock(uuid4(), "72,201,282 50,744,682 42.28% 44,121,248", page=7, ordinal=11),
        FakeBlock(uuid4(), "的净利润", page=7, ordinal=12),
    ]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]

    observations = await provider.extract(_request())

    assert len(observations) == 2
    assert observations[0].metric_code == MetricCode.NET_PROFIT_PARENT
    assert observations[0].value_text == "72,201,282"
    assert observations[1].value_text == "50,744,682"
    assert observations[1].period_end == date(2023, 12, 31)


@pytest.mark.asyncio
async def test_extract_tail_numbers_fragment_reconstruction() -> None:
    """模式 2：head 碎片 + (tail+numbers) 行（"的扣非…净利润 64,507,864 ..."）。"""
    blocks = [
        FakeBlock(uuid4(), "归属于上市公司股东", page=7, ordinal=40),
        FakeBlock(
            uuid4(), "的扣除非经常性损益后的净利润 64,507,864 44,992,919 43.37%", page=7, ordinal=41
        ),
        FakeBlock(uuid4(), "的净利润", page=7, ordinal=42),
    ]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]

    observations = await provider.extract(_request())

    assert len(observations) == 2
    assert observations[0].metric_code == MetricCode.NET_PROFIT_PARENT_EXCL_NONRECURRING
    assert observations[0].value_text == "64,507,864"
    assert observations[1].value_text == "44,992,919"


@pytest.mark.asyncio
async def test_extract_dedupe_same_metric_period() -> None:
    """同 (metric, period) 多表重复 → 保留首个（主表值不被分部表覆盖）。"""
    blocks = [
        FakeBlock(uuid4(), "营业收入 423,701,834 362,012,554", page=7, ordinal=20),
        FakeBlock(
            uuid4(), "营业收入 84,704,589 94,181,664 104,185,734 140,629,847", page=8, ordinal=30
        ),
    ]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]

    observations = await provider.extract(_request())

    assert len(observations) == 2
    assert observations[0].value_text == "423,701,834"
    assert observations[0].quote_block_id == blocks[0].block_id


@pytest.mark.asyncio
async def test_extract_two_column_annual_report() -> None:
    """双列年报行：本期 + 上期各一条观测（value span 精确定位）。"""
    text = "营业收入 45,678,901.23 43,210,987.65"
    blocks = [FakeBlock(uuid4(), text, page=7, ordinal=1)]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]

    observations = await provider.extract(_request())

    assert len(observations) == 2
    current = observations[0]
    prior = observations[1]
    assert current.metric_code == MetricCode.REVENUE
    assert current.value_text == "45,678,901.23"
    assert current.period_end == date(2024, 12, 31)
    assert current.period_start == date(2024, 1, 1)
    assert current.value_start == text.index("45,678,901.23")
    assert prior.value_text == "43,210,987.65"
    assert prior.period_end == date(2023, 12, 31)
    assert prior.period_start == date(2023, 1, 1)
    # quote = 整行（block 切片），含两个数字。
    assert observations[0].quote_text == text
    assert observations[0].quote_start == 0
    assert observations[0].quote_end == len(text)


@pytest.mark.asyncio
async def test_extract_balance_sheet_instant_period() -> None:
    text = "资产总计 5,000,000,000.00 4,500,000,000.00"
    blocks = [FakeBlock(uuid4(), text, page=9, ordinal=1)]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]

    observations = await provider.extract(_request())

    assert len(observations) == 2
    assert observations[0].metric_code == MetricCode.TOTAL_ASSETS
    assert observations[0].period_start is None
    assert observations[0].period_end == date(2024, 12, 31)
    assert observations[1].period_end == date(2023, 12, 31)


@pytest.mark.asyncio
async def test_extract_page_unit_detection() -> None:
    """页级单位标注（单位：万元）→ 该页科目行 raw_unit。"""
    blocks = [
        FakeBlock(uuid4(), "单位：万元", page=7, ordinal=1),
        FakeBlock(uuid4(), "营业成本 123,456.78 100,000.00", page=7, ordinal=2),
    ]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]

    observations = await provider.extract(_request())

    assert len(observations) == 2
    assert all(obs.raw_unit == RawUnit.TEN_THOUSAND_YUAN for obs in observations)
    assert observations[0].metric_code == MetricCode.OPERATING_COST


@pytest.mark.asyncio
async def test_extract_no_blocks_returns_empty() -> None:
    provider = StatementLineExtractionProvider(FakeSessionMaker([]))  # type: ignore[arg-type]
    assert await provider.extract(_request()) == []


@pytest.mark.asyncio
async def test_extract_skips_non_metric_lines() -> None:
    blocks = [
        FakeBlock(uuid4(), "一、经营情况讨论与分析", page=1, ordinal=1),
        FakeBlock(
            uuid4(), "本公司董事会及全体董事保证本报告内容不存在任何虚假记载", page=1, ordinal=2
        ),
    ]
    provider = StatementLineExtractionProvider(FakeSessionMaker(blocks))  # type: ignore[arg-type]
    assert await provider.extract(_request()) == []
