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
from app.evidence.extractor.quote import resolve_exact_quote

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
