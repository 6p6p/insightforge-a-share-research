"""Evidence locator projection unit tests (stage 3C.1).

校验 project_evidence_locator_refs：
- chunk.text 由每个 ref 对应 block slice 以 "\\n" 连接，sum(segment
  lengths) + separators == len(chunk.text) 的 invariant 破坏 →
  EvidenceLocatorIntegrityError（**不自动修复**）；
- 只保存 quote 实际覆盖到的 refs，char_start/end 缩窄到原 ParsedBlock
  对应字符范围，locator 原样保留（HTML xpath/element_id；PDF
  page_number/bbox）；
- 单/多 HTML block、quote 跨 "\\n"、单/多页 PDF、partial block slice、
  确定性。
"""

import pytest

from app.evidence.contracts import project_evidence_locator_refs
from app.evidence.errors import EvidenceLocatorIntegrityError

_B1 = "贵州茅台2024年营业收入"  # 13 chars
_B2 = "归属净利润862亿元"  # 10 chars


def _html_locator(ordinal: int) -> dict:
    return {
        "type": "html_dom",
        "ordinal": ordinal,
        "tag": "p",
        "xpath": f"/html/body/article/p[{ordinal}]",
        "element_id": None,
    }


def _pdf_locator(page: int, line: int, bbox: list) -> dict:
    return {
        "type": "pdf_page",
        "page_number": page,
        "line_index": line,
        "bbox": bbox,
        "page_width": 595.0,
        "page_height": 842.0,
    }


def _ref(ordinal: int, start: int, end: int, locator: dict) -> dict:
    return {"block_ordinal": ordinal, "char_start": start, "char_end": end, "locator": locator}


def _full_block_refs(blocks: list[str]) -> list[dict]:
    refs: list[dict] = []
    for index, block in enumerate(blocks, start=1):
        refs.append(_ref(index, 0, len(block), _html_locator(index)))
    return refs


# ---------------------------------------------------------------- HTML blocks


def test_single_html_block_quote_narrowed_within_block() -> None:
    refs = _full_block_refs([_B1])
    chunk_text = _B1
    projected = project_evidence_locator_refs(chunk_text, refs, quote_start=2, quote_end=6)
    assert projected == [
        _ref(1, 2, 6, _html_locator(1)),
    ]
    # quote 文本 = 原 block 切片：chunk_text[2:6] == _B1[2:6]
    assert chunk_text[2:6] == _B1[2:6]


def test_multi_html_block_quote_within_second_block() -> None:
    refs = _full_block_refs([_B1, _B2])
    chunk_text = _B1 + "\n" + _B2
    # second block 的 local span：起点 = len(_B1) + 1（"\n"）
    base = len(_B1) + 1
    projected = project_evidence_locator_refs(
        chunk_text, refs, quote_start=base + 2, quote_end=base + 6
    )
    assert projected == [_ref(2, 2, 6, _html_locator(2))]
    assert chunk_text[base + 2 : base + 6] == _B2[2:6]


def test_quote_crossing_newline_separator() -> None:
    refs = _full_block_refs([_B1, _B2])
    chunk_text = _B1 + "\n" + _B2
    # quote 覆盖 block1 最后 2 字符 + "\n" + block2 前 2 字符。
    projected = project_evidence_locator_refs(
        chunk_text, refs, quote_start=len(_B1) - 2, quote_end=len(_B1) + 1 + 2
    )
    assert projected == [
        _ref(1, len(_B1) - 2, len(_B1), _html_locator(1)),
        _ref(2, 0, 2, _html_locator(2)),
    ]
    assert chunk_text[len(_B1) - 2 : len(_B1) + 1 + 2] == _B1[-2:] + "\n" + _B2[:2]


def test_quote_full_chunk_projects_all_refs_unchanged() -> None:
    refs = _full_block_refs([_B1, _B2])
    chunk_text = _B1 + "\n" + _B2
    projected = project_evidence_locator_refs(chunk_text, refs, 0, len(chunk_text))
    assert projected == refs


def test_partial_block_slice_quote_narrowed_to_original_range() -> None:
    # 单个 ref 覆盖 block 的 [0, 20) 子区间（分块段的 partial slice）。
    locator = _pdf_locator(1, 3, [50.0, 100.0, 200.0, 120.0])
    refs = [_ref(1, 0, 20, locator)]
    chunk_text = "abcdefghij" + "klmnopqrst"  # 20 chars
    projected = project_evidence_locator_refs(chunk_text, refs, quote_start=5, quote_end=12)
    assert projected == [_ref(1, 5, 12, locator)]


# ---------------------------------------------------------------- PDF pages


def test_pdf_single_page_quote_keeps_page_bbox_locator() -> None:
    locator = _pdf_locator(1, 2, [30.0, 200.0, 300.0, 220.0])
    refs = [_ref(1, 0, len(_B1), locator)]
    projected = project_evidence_locator_refs(_B1, refs, quote_start=3, quote_end=9)
    assert projected == [_ref(1, 3, 9, locator)]


def test_pdf_multi_page_quote_across_pages_keeps_both_page_locators() -> None:
    page1 = _pdf_locator(1, 2, [30.0, 200.0, 300.0, 220.0])
    page2 = _pdf_locator(2, 1, [30.0, 80.0, 300.0, 100.0])
    refs = [_ref(1, 0, len(_B1), page1), _ref(2, 0, len(_B2), page2)]
    chunk_text = _B1 + "\n" + _B2
    projected = project_evidence_locator_refs(
        chunk_text, refs, quote_start=len(_B1) - 2, quote_end=len(_B1) + 1 + 2
    )
    assert projected == [
        _ref(1, len(_B1) - 2, len(_B1), page1),
        _ref(2, 0, 2, page2),
    ]


# ---------------------------------------------------------------- deterministic


def test_projection_is_deterministic() -> None:
    refs = _full_block_refs([_B1, _B2])
    chunk_text = _B1 + "\n" + _B2
    a = project_evidence_locator_refs(chunk_text, refs, quote_start=2, quote_end=20)
    b = project_evidence_locator_refs(chunk_text, refs, quote_start=2, quote_end=20)
    assert a == b
    assert [ref["locator"] for ref in a] == [_html_locator(1), _html_locator(2)]


def test_projection_empty_for_whitespace_only_quote() -> None:
    # quote 只覆盖 "\n" separator：无 ref 被覆盖 → []（Service 在更早的
    # derive_quote_text 阶段拒绝空白 quote，此处只记录纯函数行为）。
    refs = _full_block_refs([_B1, _B2])
    chunk_text = _B1 + "\n" + _B2
    assert project_evidence_locator_refs(chunk_text, refs, len(_B1), len(_B1) + 1) == []


# ---------------------------------------------------------------- integrity


def test_malformed_length_raises_integrity_error() -> None:
    # refs 声明段总长 5 + 1 separator = 6 != len(chunk_text) = 10。
    refs = [
        _ref(1, 0, 3, _html_locator(1)),
        _ref(2, 0, 2, _html_locator(2)),
    ]
    with pytest.raises(EvidenceLocatorIntegrityError):
        project_evidence_locator_refs("abcdefghij", refs, 0, 10)


def test_empty_refs_raise_integrity_error() -> None:
    with pytest.raises(EvidenceLocatorIntegrityError):
        project_evidence_locator_refs("abc", [], 0, 3)


def test_non_list_refs_raise_integrity_error() -> None:
    with pytest.raises(EvidenceLocatorIntegrityError):
        project_evidence_locator_refs("abc", [1, 2, 3], 0, 3)


def test_bad_ref_structure_raises_integrity_error() -> None:
    chunk_text = "abcdef"
    valid = _ref(1, 0, 6, _html_locator(1))
    bad_variants = [
        {"block_ordinal": 0, "char_start": 0, "char_end": 6, "locator": _html_locator(1)},
        {"block_ordinal": "1", "char_start": 0, "char_end": 6, "locator": _html_locator(1)},
        {"block_ordinal": 1, "char_start": -1, "char_end": 6, "locator": _html_locator(1)},
        {"block_ordinal": 1, "char_start": 0, "char_end": 0, "locator": _html_locator(1)},
        {"block_ordinal": 1, "char_start": 0, "char_end": 6, "locator": "not-a-dict"},
    ]
    for bad in bad_variants:
        with pytest.raises(EvidenceLocatorIntegrityError):
            project_evidence_locator_refs(chunk_text, [valid, bad], 0, 6)
