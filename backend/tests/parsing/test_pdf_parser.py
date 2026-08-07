"""Deterministic PDF parser unit tests (stage 2E.2).

纯函数（不联网、不访问 DB），PDF bytes 由 tests/pdf_fixtures.py 纯 stdlib
确定性构造。覆盖：
- 单页/多页顺序、page_number/line_index 1-based、line_index 每页重置；
- locator bbox 边界与 float round(...,3)；
- 重复字符 dedupe、中文提取、空页允许、整篇无文本 → PdfTextUnavailable；
- malformed → PdfParseError、加密 → PdfEncryptedError、页数/字符超限 →
  PdfResourceLimitError；
- metadata Title（normalize 后非空否则 None）/ published_at 恒 None；
- 同一 raw → 完全确定（多遍一致）；
- 跨页相邻相同行去重（保持 ParsedDocument 契约不变量）。
"""

import hashlib

import pytest

import app.parsing.pdf_parser as pdf_parser_mod
from app.parsing.contracts import PDF_PARSER_NAME, PDF_PARSER_VERSION, ParsedDocument
from app.parsing.errors import (
    PdfEncryptedError,
    PdfParseError,
    PdfResourceLimitError,
    PdfTextUnavailable,
)
from app.parsing.pdf_parser import MAX_PDF_PAGE_COUNT, MAX_PDF_TOTAL_CHARS, parse_pdf_bytes
from tests.pdf_fixtures import (
    build_pdf,
    chinese_pdf,
    duplicate_chars_pdf,
    duplicate_line_across_pages_pdf,
    empty_page_then_text_pdf,
    encrypted_pdf,
    multi_page_pdf,
    single_page_pdf,
)

_PAGE_WIDTH = 612.0
_PAGE_HEIGHT = 792.0


def _assert_round3(value: float) -> None:
    """断言是 float 且 round(...,3) 后不变（float rounding 到 3 位小数）。"""
    assert isinstance(value, float)
    assert round(value, 3) == value


def _assert_bbox(bbox: list) -> None:
    x0, top, x1, bottom = bbox
    assert isinstance(bbox, list) and len(bbox) == 4
    for value in bbox:
        _assert_round3(value)
    assert 0.0 <= x0 <= x1 <= _PAGE_WIDTH
    assert 0.0 <= top <= bottom <= _PAGE_HEIGHT


# ---------------------------------------------------------------- 基本解析


def test_single_page_parses_lines_in_top_order() -> None:
    doc = parse_pdf_bytes(single_page_pdf(title="PDF 标题"))
    assert doc.parser_name == PDF_PARSER_NAME
    assert doc.parser_version == PDF_PARSER_VERSION
    assert [b.text for b in doc.blocks] == ["Hello world", "Second line", "Third line"]
    assert [b.locator["page_number"] for b in doc.blocks] == [1, 1, 1]
    assert [b.locator["line_index"] for b in doc.blocks] == [1, 2, 3]
    assert all(b.block_type.value == "paragraph" for b in doc.blocks)


def test_multi_page_order_and_line_index_reset() -> None:
    doc = parse_pdf_bytes(multi_page_pdf())
    assert [(b.locator["page_number"], b.locator["line_index"], b.text) for b in doc.blocks] == [
        (1, 1, "Page one"),
        (2, 1, "Page two"),
    ]


def test_empty_page_alone_does_not_fail() -> None:
    """单页无文字不失败；只有整个 PDF 无文本才 PdfTextUnavailable。"""
    doc = parse_pdf_bytes(empty_page_then_text_pdf())
    assert [(b.locator["page_number"], b.locator["line_index"], b.text) for b in doc.blocks] == [
        (2, 1, "Has text")
    ]


def test_whole_pdf_without_text_raises_text_unavailable() -> None:
    # 两页都是空页：整个 PDF 无文本 → PdfTextUnavailable。
    no_text = build_pdf([[], []])
    with pytest.raises(PdfTextUnavailable) as exc:
        parse_pdf_bytes(no_text)
    assert exc.value.code == "pdf_text_unavailable"


# ---------------------------------------------------------------- locator 契约


def test_locator_bbox_within_bounds_and_rounded() -> None:
    doc = parse_pdf_bytes(single_page_pdf())
    for block in doc.blocks:
        locator = block.locator
        assert locator["type"] == "pdf_page"
        assert set(locator) == {
            "type",
            "page_number",
            "line_index",
            "bbox",
            "page_width",
            "page_height",
        }
        assert locator["page_number"] == 1
        assert isinstance(locator["page_number"], int)
        assert isinstance(locator["line_index"], int)
        _assert_round3(locator["page_width"])
        _assert_round3(locator["page_height"])
        assert locator["page_width"] == _PAGE_WIDTH
        assert locator["page_height"] == _PAGE_HEIGHT
        _assert_bbox(locator["bbox"])


def test_locator_stable_across_identical_input() -> None:
    """同一 PDF bytes → 每次 locator 完全一致（float 精确相等）。"""
    raw = single_page_pdf(title="标题")
    locators = [tuple(b.locator["bbox"]) for b in parse_pdf_bytes(raw).blocks]
    for _ in range(5):
        assert [tuple(b.locator["bbox"]) for b in parse_pdf_bytes(raw).blocks] == locators


# ---------------------------------------------------------------- 内容特性


def test_duplicate_chars_deduped() -> None:
    """同一位置绘制两次 'aa'：dedupe_chars 去重后只剩一个 'aa' 行。"""
    doc = parse_pdf_bytes(duplicate_chars_pdf())
    assert [b.text for b in doc.blocks] == ["aa"]


def test_chinese_text_extracted() -> None:
    doc = parse_pdf_bytes(chinese_pdf())
    assert doc.blocks[0].text == "中文段落：确定性解析。"
    x0, top, x1, bottom = doc.blocks[0].locator["bbox"]
    assert 0.0 <= x0 < x1 <= _PAGE_WIDTH
    assert 0.0 <= top < bottom <= _PAGE_HEIGHT


def test_adjacent_identical_lines_across_pages_deduped() -> None:
    """page1 末行与 page2 首行文本相同（相邻）→ 保留首个，幸存行重编号。"""
    doc = parse_pdf_bytes(duplicate_line_across_pages_pdf())
    assert [(b.locator["page_number"], b.locator["line_index"], b.text) for b in doc.blocks] == [
        (1, 1, "Header"),
        (1, 2, "Dup"),
        (2, 1, "Body two"),
    ]


def test_metadata_title_and_published_at() -> None:
    doc = parse_pdf_bytes(single_page_pdf(title="  季度  报告  "))
    assert doc.extracted_title == "季度 报告"  # normalize 连续空白
    assert doc.extracted_published_at is None  # 绝不使用 CreationDate/ModDate


def test_metadata_title_blank_treated_as_none() -> None:
    doc = parse_pdf_bytes(single_page_pdf(title="   "))
    assert doc.extracted_title is None
    assert doc.extracted_published_at is None


def test_metadata_missing_title_none() -> None:
    doc = parse_pdf_bytes(single_page_pdf())  # 无 Info Title
    assert doc.extracted_title is None


def test_document_blocks_contract_satisfied() -> None:
    """parser 输出必须通过 ParsedDocument 契约（ordinal 连续、相邻不同、文本已 normalize）。"""
    doc = parse_pdf_bytes(multi_page_pdf())
    # 契约校验在 ParsedDocument.__post_init__ 完成；此处再显式复核。
    assert isinstance(doc, ParsedDocument)
    assert [b.ordinal for b in doc.blocks] == list(range(1, len(doc.blocks) + 1))
    for block in doc.blocks:
        assert block.text == " ".join(block.text.split())
        assert block.text_sha256 == hashlib.sha256(block.text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- 安全边界 / 错误


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"not a pdf at all",
        b"%PDF-1.4\n%% this is corrupt below",
        b"\x00\x01\x02%PDF-1.4",
    ],
)
def test_invalid_magic_or_malformed_raises_parse_error(raw: bytes) -> None:
    with pytest.raises(PdfParseError) as exc:
        parse_pdf_bytes(raw)
    assert exc.value.code == "pdf_parse_error"


def test_malformed_pdf_with_valid_magic_raises_parse_error() -> None:
    # 合法的 %PDF- 头但对象/xref 结构损坏。
    malformed = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
    with pytest.raises(PdfParseError) as exc:
        parse_pdf_bytes(malformed)
    assert exc.value.code == "pdf_parse_error"


def test_encrypted_pdf_raises_encrypted_error() -> None:
    with pytest.raises(PdfEncryptedError) as exc:
        parse_pdf_bytes(encrypted_pdf())
    assert exc.value.code == "pdf_encrypted_error"


def test_page_count_over_limit_raises_resource_error(monkeypatch) -> None:
    three_page = build_pdf([[(72.0, 720.0, "A")], [(72.0, 720.0, "B")], [(72.0, 720.0, "C")]])
    monkeypatch.setattr(pdf_parser_mod, "MAX_PDF_PAGE_COUNT", 2)
    with pytest.raises(PdfResourceLimitError) as exc:
        parse_pdf_bytes(three_page)
    assert exc.value.code == "pdf_resource_limit_exceeded"


def test_zero_page_pdf_raises_resource_error() -> None:
    zero_page = build_pdf([])
    with pytest.raises(PdfResourceLimitError):
        parse_pdf_bytes(zero_page)


def test_total_chars_over_limit_raises_resource_error(monkeypatch) -> None:
    monkeypatch.setattr(pdf_parser_mod, "MAX_PDF_TOTAL_CHARS", 5)
    with pytest.raises(PdfResourceLimitError) as exc:
        parse_pdf_bytes(single_page_pdf())
    assert exc.value.code == "pdf_resource_limit_exceeded"


def test_page_count_limit_boundary_accepted(monkeypatch) -> None:
    """恰好等于上限（2 页 vs 上限 2）合法通过。"""
    monkeypatch.setattr(pdf_parser_mod, "MAX_PDF_PAGE_COUNT", 2)
    doc = parse_pdf_bytes(multi_page_pdf())
    assert [b.text for b in doc.blocks] == ["Page one", "Page two"]


def test_parser_does_not_consume_input_stream() -> None:
    """parse_pdf_bytes 接收 bytes，不要求外部文件/流生命周期（无临时文件）。"""
    raw = single_page_pdf()
    parse_pdf_bytes(raw)
    assert isinstance(raw, bytes) and raw.startswith(b"%PDF-")


def test_parser_accepts_bytesio_only_via_internal_path() -> None:
    """内部用 BytesIO 包装 bytes；不写任何临时外部文件。"""
    raw = single_page_pdf()
    doc = parse_pdf_bytes(raw)
    assert doc.raw_content_sha256 == hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------- 确定性


def test_parse_is_deterministic_across_runs() -> None:
    raw = single_page_pdf(title="标题")
    first = parse_pdf_bytes(raw)
    for _ in range(10):
        assert parse_pdf_bytes(raw) == first
    assert first.raw_content_sha256 == hashlib.sha256(raw).hexdigest()


def test_module_constants_defaults() -> None:
    """默认资源限制必须与规范一致（1000 页 / 500 万字符）。"""
    assert MAX_PDF_PAGE_COUNT == 1000
    assert MAX_PDF_TOTAL_CHARS == 5_000_000
