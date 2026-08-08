"""Evidence quote slicing contract unit tests (stage 3C.1).

校验 quote_text = chunk.text[quote_start:quote_end] 的程序切片契约：
精确子串（含中文按 code point 索引）、越界 / 区间非法 → EvidenceQuoteRangeError、
空白 quote 拒绝、quote_sha256 确定性。
"""

import hashlib

import pytest

from app.evidence.contracts import compute_quote_sha256, derive_quote_text
from app.evidence.errors import EvidenceQuoteRangeError

_CHINESE_CHUNK = "贵州茅台2024年实现营业收入1709亿元，归属净利润862亿元。"
_ASCII_CHUNK = "hello evidence world"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_exact_substring_roundtrip_ascii() -> None:
    quote = derive_quote_text(chunk_text=_ASCII_CHUNK, quote_start=6, quote_end=14)
    assert quote == _ASCII_CHUNK[6:14]


def test_exact_substring_chinese_char_index() -> None:
    # Python str 索引按 Unicode code point：中文逐字索引。
    quote = derive_quote_text(chunk_text=_CHINESE_CHUNK, quote_start=0, quote_end=4)
    assert quote == "贵州茅台"
    assert quote == _CHINESE_CHUNK[0:4]


def test_quote_spans_surrogate_free_cjk_and_digits() -> None:
    start = _CHINESE_CHUNK.index("营业收入")
    end = start + len("营业收入1709亿元")
    quote = derive_quote_text(chunk_text=_CHINESE_CHUNK, quote_start=start, quote_end=end)
    assert quote == "营业收入1709亿元"


def test_quote_returns_full_text_when_full_range() -> None:
    quote = derive_quote_text(
        chunk_text=_CHINESE_CHUNK, quote_start=0, quote_end=len(_CHINESE_CHUNK)
    )
    assert quote == _CHINESE_CHUNK


def test_quote_preserves_inner_whitespace_and_punctuation() -> None:
    # quote 是原文切片：绝不 normalize / 改写 / 摘要 / 自动纠错。
    text = "a  b\tc；，。"
    quote = derive_quote_text(chunk_text=text, quote_start=0, quote_end=len(text))
    assert quote == text


def test_quote_out_of_range_high() -> None:
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(chunk_text=_ASCII_CHUNK, quote_start=0, quote_end=len(_ASCII_CHUNK) + 1)


def test_quote_start_beyond_text() -> None:
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(
            chunk_text=_ASCII_CHUNK, quote_start=len(_ASCII_CHUNK), quote_end=len(_ASCII_CHUNK) + 1
        )


def test_quote_negative_start_rejected() -> None:
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(chunk_text=_ASCII_CHUNK, quote_start=-1, quote_end=5)


def test_quote_end_lte_start_rejected() -> None:
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(chunk_text=_ASCII_CHUNK, quote_start=5, quote_end=5)


def test_quote_whitespace_only_rejected() -> None:
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(chunk_text="  \t  ", quote_start=0, quote_end=3)


def test_quote_mid_whitespace_slice_rejected() -> None:
    # quote 切片结果只含空白 → 拒绝（quote 不能只含空白）。
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(chunk_text="a\n\nb", quote_start=1, quote_end=3)


def test_quote_bool_or_non_int_range_rejected() -> None:
    with pytest.raises(EvidenceQuoteRangeError):
        derive_quote_text(chunk_text=_ASCII_CHUNK, quote_start=True, quote_end=5)


def test_quote_sha256_matches_utf8_hash() -> None:
    assert compute_quote_sha256(_CHINESE_CHUNK) == _sha(_CHINESE_CHUNK)
    assert compute_quote_sha256("净利润增长") == _sha("净利润增长")
