"""P1 recovery 单元测试：缺口分类 + 财务原文确定性解析（0 LLM / 0 DB）。"""

import pytest

from app.research_backflow.recovery import (
    MODEL_ASSISTED_RECOVERY_MARKER,
    FinancialRecoveryCandidate,
    GapClass,
    alias_matches,
    classify_gap,
    locate_candidates,
    parse_quote_value,
    recovery_exhausted,
)


class TestClassifyGap:
    def test_source_gap_when_no_source(self) -> None:
        assert (
            classify_gap(has_source=False, has_chunk=False, has_evidence=False)
            is GapClass.SOURCE_GAP
        )

    def test_retrieval_miss_when_source_without_chunk(self) -> None:
        assert (
            classify_gap(has_source=True, has_chunk=False, has_evidence=False)
            is GapClass.RETRIEVAL_MISS
        )

    def test_extraction_miss_when_chunk_without_evidence(self) -> None:
        assert (
            classify_gap(has_source=True, has_chunk=True, has_evidence=False)
            is GapClass.EXTRACTION_MISS
        )

    def test_evidence_present_raises(self) -> None:
        with pytest.raises(ValueError):
            classify_gap(has_source=True, has_chunk=True, has_evidence=True)

    def test_recovery_exhausted_only_after_all_three_fail(self) -> None:
        attempted = [
            ("existing_source", False),
            ("financial", False),
            ("supplementary", False),
        ]
        assert recovery_exhausted(attempted)
        # 任一方法未尝试 → 未穷尽（继续找，不臆断不存在）。
        assert not recovery_exhausted(attempted[:2])
        # 任一恢复成功 → 未穷尽。
        assert not recovery_exhausted(
            [("existing_source", True), ("financial", False), ("supplementary", False)]
        )


class TestParseQuoteValue:
    def test_plain_number(self) -> None:
        value = parse_quote_value("2026年营业收入约1500亿元")
        assert value is not None
        assert value.number == 1500
        assert value.raw_unit == "hundred_million_yuan"
        assert value.matched_text == "1500"

    def test_wan_unit(self) -> None:
        value = parse_quote_value("归母净利润1,234.56万元")
        assert value is not None
        assert value.number == 1234.56
        assert value.raw_unit == "ten_thousand_yuan"

    def test_negative_number(self) -> None:
        value = parse_quote_value("同比下降-3.2亿元")
        assert value is not None
        assert value.number == -3.2
        assert value.raw_unit == "hundred_million_yuan"

    def test_ratio_without_unit(self) -> None:
        value = parse_quote_value("净利率为50%")
        assert value is not None
        assert value.number == 50
        assert value.raw_unit is None

    def test_no_number_returns_none(self) -> None:
        assert parse_quote_value("无相关数字披露") is None

    def test_thousand_unit(self) -> None:
        value = parse_quote_value("每股收益2.35千元")
        assert value is not None
        assert value.raw_unit == "thousand_yuan"

    def test_yuan_unit(self) -> None:
        value = parse_quote_value("分红每股0.8元")
        assert value is not None
        assert value.number == 0.8
        assert value.raw_unit == "yuan"


class TestAliasAndLocate:
    def test_alias_matches_chinese(self) -> None:
        assert alias_matches("公司披露营业收入情况", ["营业收入", "营收"])
        assert alias_matches("Revenue", ["revenue"])
        assert not alias_matches("净利润披露", ["营业收入"])

    def test_locate_candidates_from_real_blocks(self) -> None:
        blocks = [
            "本报告期公司实现营业收入1500亿元，同比增长15%。",
            "公司经营现金流净额为320亿元。",
            "董事会审议通过利润分配方案。",
        ]
        candidates = locate_candidates(["营业收入", "营收"], blocks)
        assert len(candidates) >= 1
        first = candidates[0]
        assert isinstance(first, FinancialRecoveryCandidate)
        assert first.block_index == 0
        assert "营业收入1500亿元" in first.quote  # quote 来自真实原文
        assert first.value.number == 1500
        assert first.value.raw_unit == "hundred_million_yuan"

    def test_locate_skips_blocks_without_number(self) -> None:
        blocks = ["公司不存在相关披露。", "公司财务稳健。"]
        assert locate_candidates(["财务"], blocks) == []

    def test_marker_is_internal_constant(self) -> None:
        assert MODEL_ASSISTED_RECOVERY_MARKER == "model_assisted_recovery"
