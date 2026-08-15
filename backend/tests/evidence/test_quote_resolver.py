"""Exact quote resolver unit tests (stage 3C.2).

resolve_exact_quote 把 LLM 返回的 quote_text 解析成 chunk.text 的精确子串
区间 [start, end)：
- 唯一出现一次 → (start, end)；
- 0 次 → EvidenceExtractionQuoteNotFound；
- >1 次（含重叠）→ EvidenceExtractionQuoteAmbiguous；
- 禁止 fuzzy match / normalize 后匹配 / 自动修正标点空白；
- LLM 不返回 char offsets（offsets 全部由 resolver 推导）。
"""

import pytest

from app.evidence.extractor.errors import (
    EvidenceExtractionQuoteAmbiguous,
    EvidenceExtractionQuoteNotFound,
)
from app.evidence.extractor.quote import (
    resolve_exact_quote,
    resolve_quote_whitespace_tolerant,
)

_ZH = "公司2025年营业收入为100亿元，同比增长12%；其中茅台酒收入150亿元。"
_ASCII = "The quick brown fox jumps over the lazy dog."


def test_unique_chinese_quote_returns_span() -> None:
    quote = "营业收入为100亿元"
    start = _ZH.index(quote)
    assert resolve_exact_quote(_ZH, quote) == (start, start + len(quote))


def test_unique_ascii_quote_returns_span() -> None:
    quote = "quick brown fox"
    start = _ASCII.index(quote)
    assert resolve_exact_quote(_ASCII, quote) == (start, start + len(quote))


def test_quote_with_punctuation_exact() -> None:
    quote = "同比增长12%；"
    start = _ZH.index(quote)
    assert resolve_exact_quote(_ZH, quote) == (start, start + len(quote))


def test_quote_crossing_newline_exact() -> None:
    text = "第一行\n第二行"
    quote = "行\n第二"
    start = text.index(quote)
    assert resolve_exact_quote(text, quote) == (start, start + len(quote))


def test_full_text_quote_returns_whole_span() -> None:
    assert resolve_exact_quote(_ZH, _ZH) == (0, len(_ZH))


def test_quote_with_trailing_whitespace_matches_raw() -> None:
    # quote_text 保持逐字原文（含尾部空白）参与精确匹配，不做 strip。
    text = "a  b"
    quote = "a  "
    assert resolve_exact_quote(text, quote) == (0, 3)


def test_no_match_raises_not_found() -> None:
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        resolve_exact_quote(_ZH, "营业收入为999亿元")


def test_whitespace_difference_not_auto_corrected() -> None:
    # chunk 为 "a  b"（双空格），quote 为 "a b"（单空格）→ 精确匹配失败，
    # 不做 normalize / 自动修正空白。
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        resolve_exact_quote("a  b", "a b")


def test_punctuation_difference_not_auto_corrected() -> None:
    # chunk 用中文逗号，quote 用英文逗号 → 不自动修正标点。
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        resolve_exact_quote("收入100亿元，同比", "收入100亿元,同比")


def test_repeated_exact_quote_raises_ambiguous() -> None:
    text = "重复。重复。重复。"
    with pytest.raises(EvidenceExtractionQuoteAmbiguous):
        resolve_exact_quote(text, "重复")


def test_overlapping_repeated_quote_raises_ambiguous() -> None:
    # "ababa" 中 "aba" 在位置 0 与 2 各出现一次（重叠）→ ambiguous。
    with pytest.raises(EvidenceExtractionQuoteAmbiguous):
        resolve_exact_quote("ababa", "aba")


def test_repeated_substring_across_newline_raises_ambiguous() -> None:
    text = "贵州茅台\n贵州茅台\n归属净利润"
    with pytest.raises(EvidenceExtractionQuoteAmbiguous):
        resolve_exact_quote(text, "贵州茅台")


def test_blank_quote_raises_not_found() -> None:
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        resolve_exact_quote(_ZH, "   ")


# ------------------------------------------------------------------ 空白容差（V1.1 closure，extractor v3）


def test_tolerant_resolves_single_space_difference() -> None:
    # PDF 解析布局空白：「约 90%」vs 模型引用「约90%」。
    chunk = "公司今年上半年总体产能利用率保持在约 90%的较高水平。"
    quote = "公司今年上半年总体产能利用率保持在约90%的较高水平"
    start, end = resolve_quote_whitespace_tolerant(chunk, quote)
    # 命中区间 = 原文逐字切片（含布局空格）。
    assert chunk[start:end] == "公司今年上半年总体产能利用率保持在约 90%的较高水平"


def test_tolerant_resolves_newline_difference() -> None:
    chunk = "公司今年上半年总体产能利用率保持在约\n90%的较高水平。"
    quote = "总体产能利用率保持在约90%"
    start, end = resolve_quote_whitespace_tolerant(chunk, quote)
    assert chunk[start:end] == "总体产能利用率保持在约\n90%"


def test_tolerant_exact_match_still_works() -> None:
    chunk = "营业收入为100亿元"
    start, end = resolve_quote_whitespace_tolerant(chunk, "营业收入为100亿元")
    assert (start, end) == (0, len(chunk))


def test_tolerant_non_whitespace_difference_still_rejected() -> None:
    # 数字/标点差异（非空白）→ 仍然拒绝（只允许空白差异）。
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        resolve_quote_whitespace_tolerant("收入100亿元，同比", "收入100亿元,同比")


def test_tolerant_missing_content_still_rejected() -> None:
    with pytest.raises(EvidenceExtractionQuoteNotFound):
        resolve_quote_whitespace_tolerant(_ZH, "营业收入为999亿元")


def test_tolerant_repeated_quote_raises_ambiguous() -> None:
    with pytest.raises(EvidenceExtractionQuoteAmbiguous):
        resolve_quote_whitespace_tolerant("重复。重复。", "重 复")


def test_tolerant_multi_whitespace_run_maps_to_original() -> None:
    chunk = "a    b"  # 4 空格
    start, end = resolve_quote_whitespace_tolerant(chunk, "a b")
    assert chunk[start:end] == "a    b"
