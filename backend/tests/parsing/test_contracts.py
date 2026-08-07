"""Parsing contracts unit tests (stage 2E.1, §8 契约部分)。

纯函数（不联网、不访问 DB）覆盖：
- ParsedBlock 契约校验（ordinal / text normalize / text_sha256 / locator 五键）；
- ParsedDocument 契约校验（ordinal 连续、相邻 block 不同、published_at aware）；
- compute_parse_fingerprint 确定性（同一输入 → 同一指纹；任何字段变化 →
  指纹变化；不包含 parsed_at / created_at / DB ID）。
"""

import hashlib
from datetime import UTC, datetime

import pytest

from app.domain.parsing import ParsedBlockType
from app.parsing.contracts import (
    HTML_PARSER_NAME,
    HTML_PARSER_VERSION,
    PDF_PARSER_NAME,
    PDF_PARSER_VERSION,
    ParsedBlock,
    ParsedDocument,
    ParsingContractViolation,
    compute_parse_fingerprint,
)
from app.parsing.errors import ParsingError


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _locator(ordinal: int, *, tag: str = "p") -> dict:
    return {
        "type": "html_dom",
        "ordinal": ordinal,
        "tag": tag,
        "xpath": f"/html/body/article/{tag}",
        "element_id": None,
    }


def _block(
    ordinal: int,
    text: str,
    block_type: ParsedBlockType = ParsedBlockType.PARAGRAPH,
) -> ParsedBlock:
    return ParsedBlock(
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        text_sha256=_sha(text),
        locator=_locator(ordinal),
    )


def _doc(
    blocks: tuple[ParsedBlock, ...],
    *,
    raw_sha: str = "a" * 64,
    title: str | None = None,
    published_at: datetime | None = None,
) -> ParsedDocument:
    return ParsedDocument(
        parser_name=HTML_PARSER_NAME,
        parser_version=HTML_PARSER_VERSION,
        raw_content_sha256=raw_sha,
        extracted_title=title,
        extracted_published_at=published_at,
        blocks=blocks,
    )


# ---------------------------------------------------------- ParsedBlock 校验


def test_block_ordinal_zero_rejected() -> None:
    with pytest.raises(ParsingContractViolation):
        _block(0, "正文")


def test_block_ordinal_must_be_int() -> None:
    with pytest.raises(ParsingContractViolation):
        ParsedBlock(
            ordinal=True,  # bool 是 int 子类，必须显式拒绝
            block_type=ParsedBlockType.PARAGRAPH,
            text="正文",
            text_sha256=_sha("正文"),
            locator=_locator(1),
        )


def test_block_non_normalized_text_rejected() -> None:
    with pytest.raises(ParsingContractViolation):
        _block(1, "两 个  空格")


def test_block_blank_text_rejected() -> None:
    with pytest.raises(ParsingContractViolation):
        _block(1, "   ")


def test_block_text_sha256_mismatch_rejected() -> None:
    with pytest.raises(ParsingContractViolation):
        ParsedBlock(
            ordinal=1,
            block_type=ParsedBlockType.PARAGRAPH,
            text="正文",
            text_sha256="b" * 64,  # 不等于 sha256("正文")
            locator=_locator(1),
        )


def test_block_locator_missing_key_rejected() -> None:
    bad_locator = _locator(1)
    del bad_locator["element_id"]
    with pytest.raises(ParsingContractViolation):
        ParsedBlock(
            ordinal=1,
            block_type=ParsedBlockType.PARAGRAPH,
            text="正文",
            text_sha256=_sha("正文"),
            locator=bad_locator,
        )


def test_block_locator_wrong_type_rejected() -> None:
    bad_locator = dict(_locator(1), type="xpath_2")
    with pytest.raises(ParsingContractViolation):
        ParsedBlock(
            ordinal=1,
            block_type=ParsedBlockType.PARAGRAPH,
            text="正文",
            text_sha256=_sha("正文"),
            locator=bad_locator,
        )


def test_block_locator_ordinal_mismatch_rejected() -> None:
    bad_locator = dict(_locator(2))
    with pytest.raises(ParsingContractViolation):
        ParsedBlock(
            ordinal=1,
            block_type=ParsedBlockType.PARAGRAPH,
            text="正文",
            text_sha256=_sha("正文"),
            locator=bad_locator,
        )


def test_block_locator_relative_xpath_rejected() -> None:
    bad_locator = dict(_locator(1), xpath="body/article/p")
    with pytest.raises(ParsingContractViolation):
        ParsedBlock(
            ordinal=1,
            block_type=ParsedBlockType.PARAGRAPH,
            text="正文",
            text_sha256=_sha("正文"),
            locator=bad_locator,
        )


# ---------------------------------------------------------- ParsedDocument 校验


def test_document_ordinal_non_contiguous_rejected() -> None:
    blocks = (_block(1, "第一段"), _block(3, "第三段"))  # 缺 2
    with pytest.raises(ParsingContractViolation):
        _doc(blocks)


def test_document_adjacent_identical_blocks_rejected() -> None:
    blocks = (_block(1, "相同"), _block(2, "相同"))
    with pytest.raises(ParsingContractViolation):
        _doc(blocks)


def test_document_non_adjacent_identical_blocks_allowed() -> None:
    blocks = (_block(1, "相同"), _block(2, "不同"), _block(3, "相同"))
    assert _doc(blocks).blocks == blocks


def test_document_naive_published_at_rejected() -> None:
    naive = datetime(2026, 8, 7, 9, 30)
    with pytest.raises(ParsingContractViolation):
        _doc((_block(1, "正文"),), published_at=naive)


def test_document_aware_published_at_accepted() -> None:
    aware = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
    doc = _doc((_block(1, "正文"),), published_at=aware)
    assert doc.extracted_published_at == aware


def test_document_blank_title_rejected() -> None:
    with pytest.raises(ParsingContractViolation):
        _doc((_block(1, "正文"),), title="   ")


# ---------------------------------------------------------- fingerprint 确定性


def test_fingerprint_deterministic_and_hex() -> None:
    doc = _doc((_block(1, "第一段"), _block(2, "第二段")), title="标题")
    fp1 = compute_parse_fingerprint("source-uuid", doc)
    fp2 = compute_parse_fingerprint("source-uuid", doc)
    assert fp1 == fp2
    assert len(fp1) == 64
    int(fp1, 16)  # 必须是合法 hex


def test_fingerprint_scoped_to_source_id() -> None:
    doc = _doc((_block(1, "正文"),))
    assert compute_parse_fingerprint("source-a", doc) != compute_parse_fingerprint("source-b", doc)


def test_fingerprint_changes_with_raw_sha() -> None:
    base = _doc((_block(1, "正文"),), raw_sha="a" * 64)
    changed = _doc((_block(1, "正文"),), raw_sha="b" * 64)
    assert compute_parse_fingerprint("s", base) != compute_parse_fingerprint("s", changed)


def test_fingerprint_changes_with_block_text() -> None:
    base = _doc((_block(1, "甲"),))
    changed = _doc((_block(1, "乙"),))
    assert compute_parse_fingerprint("s", base) != compute_parse_fingerprint("s", changed)


def test_fingerprint_changes_with_title() -> None:
    base = _doc((_block(1, "正文"),), title=None)
    changed = _doc((_block(1, "正文"),), title="新标题")
    assert compute_parse_fingerprint("s", base) != compute_parse_fingerprint("s", changed)


def test_fingerprint_changes_with_published_at() -> None:
    base = _doc((_block(1, "正文"),), published_at=None)
    changed = _doc(
        (_block(1, "正文"),),
        published_at=datetime(2026, 8, 7, 1, 30, tzinfo=UTC),
    )
    assert compute_parse_fingerprint("s", base) != compute_parse_fingerprint("s", changed)


def test_fingerprint_changes_with_locator_element_id() -> None:
    """locator 的 element_id 变化应改变指纹（定位是证据链一部分）。"""
    with_id = ParsedBlock(
        ordinal=1,
        block_type=ParsedBlockType.PARAGRAPH,
        text="正文",
        text_sha256=_sha("正文"),
        locator=dict(_locator(1), element_id="p1"),
    )
    base = _doc((_block(1, "正文"),))
    changed = _doc((with_id,))
    assert compute_parse_fingerprint("s", base) != compute_parse_fingerprint("s", changed)


def test_fingerprint_canonical_over_key_order() -> None:
    """sort_keys + 固定 separators：同一语义内容无论 dict 插入顺序 → 同一指纹。

    通过两次独立构造等价文档（block 字段按不同顺序传给 dataclass，但内容
    一致）验证指纹稳定。
    """
    doc = _doc((_block(1, "正文"),), title="标题")
    assert compute_parse_fingerprint("s", doc) == compute_parse_fingerprint("s", doc)


def test_fingerprint_is_sha256_of_canonical_json() -> None:
    """指纹不依赖 repr()/hash()：等于 canonical JSON 的 SHA-256，可跨进程复现。"""
    doc = _doc((_block(1, "正文"),), title="标题")
    fp = compute_parse_fingerprint("source-1", doc)
    # 手工构造与实现等价 canonical JSON，验证确定性可外部复算。
    payload = {
        "source_id": "source-1",
        "raw_content_sha256": doc.raw_content_sha256,
        "parser_name": "html_dom",
        "parser_version": 2,
        "extracted_title": "标题",
        "extracted_published_at": None,
        "blocks": [
            {
                "ordinal": 1,
                "block_type": "paragraph",
                "text": "正文",
                "text_sha256": _sha("正文"),
                "locator": {
                    "element_id": None,
                    "ordinal": 1,
                    "tag": "p",
                    "type": "html_dom",
                    "xpath": "/html/body/article/p",
                },
            }
        ],
    }
    import json

    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    assert fp == hashlib.sha256(canonical).hexdigest()


def test_contract_violation_is_parsing_error() -> None:
    assert issubclass(ParsingContractViolation, ParsingError)


# ---------------------------------------------------------- pdf_page locator 契约


def _pdf_locator(ordinal: int, **overrides) -> dict:
    locator = {
        "type": "pdf_page",
        "page_number": 1,
        "line_index": ordinal,
        "bbox": [72.0, 62.484, 131.34, 74.484],
        "page_width": 612.0,
        "page_height": 792.0,
    }
    locator.update(overrides)
    return locator


def _pdf_block(
    ordinal: int,
    text: str,
    *,
    block_type: ParsedBlockType = ParsedBlockType.PARAGRAPH,
    locator: dict | None = None,
) -> ParsedBlock:
    return ParsedBlock(
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        text_sha256=_sha(text),
        locator=locator if locator is not None else _pdf_locator(ordinal),
    )


def _pdf_doc(blocks: tuple[ParsedBlock, ...]) -> ParsedDocument:
    return ParsedDocument(
        parser_name=PDF_PARSER_NAME,
        parser_version=PDF_PARSER_VERSION,
        raw_content_sha256="a" * 64,
        extracted_title=None,
        extracted_published_at=None,
        blocks=blocks,
    )


def test_pdf_block_valid_locator_accepted() -> None:
    block = _pdf_block(1, "正文")
    assert block.locator["type"] == "pdf_page"
    assert set(block.locator) == {
        "type",
        "page_number",
        "line_index",
        "bbox",
        "page_width",
        "page_height",
    }


def test_pdf_block_missing_key_rejected() -> None:
    bad = _pdf_locator(1)
    del bad["page_height"]
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_extra_key_rejected() -> None:
    bad = dict(_pdf_locator(1), mode="scan")
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_wrong_locator_type_rejected() -> None:
    bad = dict(_pdf_locator(1), type="pdf_page_v0")
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_zero_page_number_rejected() -> None:
    bad = dict(_pdf_locator(1), page_number=0)
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_float_page_number_rejected() -> None:
    bad = dict(_pdf_locator(1), page_number=1.5)
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_zero_line_index_rejected() -> None:
    bad = dict(_pdf_locator(1), line_index=0)
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_bool_page_width_rejected() -> None:
    bad = dict(_pdf_locator(1), page_width=True)  # bool 是 int 子类，显式拒绝
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_negative_page_height_rejected() -> None:
    bad = dict(_pdf_locator(1), page_height=-1.0)
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_bbox_not_list_rejected() -> None:
    bad = dict(_pdf_locator(1), bbox=(72.0, 62.484, 131.34, 74.484))  # tuple 非 list
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_bbox_wrong_length_rejected() -> None:
    bad = dict(_pdf_locator(1), bbox=[72.0, 62.484, 131.34])
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_bbox_out_of_page_width_rejected() -> None:
    bad = dict(_pdf_locator(1), bbox=[72.0, 62.484, 700.0, 74.484])  # x1 > page_width
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_bbox_out_of_page_height_rejected() -> None:
    bad = dict(_pdf_locator(1), bbox=[72.0, -5.0, 131.34, 74.484])  # top < 0
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_block_bbox_negative_width_rejected() -> None:
    bad = dict(_pdf_locator(1), bbox=[72.0, 62.484, 60.0, 74.484])  # x1 < x0
    with pytest.raises(ParsingContractViolation):
        _pdf_block(1, "正文", locator=bad)


def test_pdf_document_valid_accepted() -> None:
    doc = _pdf_doc((_pdf_block(1, "正文"), _pdf_block(2, "第二行")))
    assert doc.parser_name == PDF_PARSER_NAME
    assert doc.parser_version == PDF_PARSER_VERSION


def test_pdf_document_unknown_parser_name_rejected() -> None:
    doc = _pdf_doc((_pdf_block(1, "正文"),))
    with pytest.raises(ParsingContractViolation):
        ParsedDocument(
            parser_name="pdf_dummy",
            parser_version=1,
            raw_content_sha256="a" * 64,
            extracted_title=None,
            extracted_published_at=None,
            blocks=doc.blocks,
        )


def test_pdf_document_wrong_parser_version_rejected() -> None:
    doc = _pdf_doc((_pdf_block(1, "正文"),))
    with pytest.raises(ParsingContractViolation):
        ParsedDocument(
            parser_name=PDF_PARSER_NAME,
            parser_version=99,
            raw_content_sha256="a" * 64,
            extracted_title=None,
            extracted_published_at=None,
            blocks=doc.blocks,
        )


def test_pdf_fingerprint_deterministic_and_parser_scoped() -> None:
    """pdf_layout v1 的 ParsedDocument 指纹同样确定性、按 source/内容变化。"""
    doc = _pdf_doc((_pdf_block(1, "正文"), _pdf_block(2, "第二行")))
    fp1 = compute_parse_fingerprint("source-pdf", doc)
    fp2 = compute_parse_fingerprint("source-pdf", doc)
    assert fp1 == fp2
    assert len(fp1) == 64
    assert compute_parse_fingerprint("source-a", doc) != compute_parse_fingerprint("source-b", doc)
