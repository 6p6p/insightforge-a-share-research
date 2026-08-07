"""Deterministic parsing contracts (stage 2E.1 / 2E.2).

ParsedBlock / ParsedDocument 是确定性 Parser（html_dom v2 / pdf_layout v2）
的统一输出契约：

- ParsedDocument 是"某一 SourceRecord + 特定 parser 版本下的解析快照"
  （不含 DB ID、不含 parsed_at/created_at），后续由 SourceParsingService
  计算 parse_fingerprint 并落库；
- ParsedBlock 带 locator（html_dom：DOM 级定位；pdf_page：页面坐标定位），
  供后续 Evidence 原文核对。

ParsedSource 是 SourceRecord 的确定性解析快照，不是 Chunk，也不是 Evidence。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.domain.parsing import ParsedBlockType
from app.parsing.errors import ParsingError

HTML_PARSER_NAME = "html_dom"
# v1 → v2：2E.1.1 收口改变了 published_at 严格识别与 charset 确定性检测语义
# （ADR-0016），旧 v1 快照不修改不删除；同 source + 同 raw + 新 version →
# 新 fingerprint → 新快照，旧快照保留（可追溯）。
HTML_PARSER_VERSION = 2

PDF_PARSER_NAME = "pdf_layout"
# v1 → v2：2E.2 正确性收口删除了 PDF 的 text-level 相邻去重（同/跨页相同
# 文本行必须全部保留，靠 page locator 区分原文位置；只有相同位置的重复绘制
# 字符由 pdfplumber dedupe_chars 去重），因此 PDF 输出语义改变，版本升为 2。
# 旧 v1 快照不修改不删除；同 source + 同 raw + 新 version → 新 fingerprint →
# 新快照，旧快照保留（可追溯）。
PDF_PARSER_VERSION = 2

# 已知 locator type → 必须精确匹配的字段集合（含 "type" 本身）。
_LOCATOR_SPECS: dict[str, set[str]] = {
    "html_dom": {"type", "ordinal", "tag", "xpath", "element_id"},
    "pdf_page": {
        "type",
        "page_number",
        "line_index",
        "bbox",
        "page_width",
        "page_height",
    },
}


def _parser_specs() -> dict[str, int]:
    """已注册 parser：parser_name → 当前 parser_version。

    动态读取当前常量（而非 import 时冻结），使测试可通过 monkeypatch 版本号
    模拟旧 parser（version bump 场景：旧快照不修改不删除，新版本新快照）。
    """
    return {
        HTML_PARSER_NAME: HTML_PARSER_VERSION,
        PDF_PARSER_NAME: PDF_PARSER_VERSION,
    }


class ParsingContractViolation(ParsingError):
    """ParsedBlock / ParsedDocument 契约校验失败（内部不变量，防御与测试用）。"""

    code = "parsing_contract_violation"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _normalized(text: str) -> bool:
    return text == " ".join(text.split())


@dataclass(frozen=True)
class ParsedBlock:
    """一段可定位的结构化文本（确定性、不可变）。

    locator 按 type 区分：
    - html_dom：{"type","ordinal","tag","xpath","element_id"}（DOM 级定位）；
    - pdf_page：{"type","page_number","line_index","bbox","page_width",
      "page_height"}（页面坐标定位，bbox=[x0,top,x1,bottom]）。
    同一 raw bytes + parser version 下完全稳定；不含绝对路径。
    """

    ordinal: int
    block_type: ParsedBlockType
    text: str
    text_sha256: str
    locator: dict

    def __post_init__(self) -> None:
        if isinstance(self.ordinal, bool) or not isinstance(self.ordinal, int):
            raise ParsingContractViolation("ordinal 必须是 int")
        if self.ordinal < 1:
            raise ParsingContractViolation("ordinal 必须 >= 1")
        if not isinstance(self.block_type, ParsedBlockType):
            raise ParsingContractViolation("block_type 必须是 ParsedBlockType")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ParsingContractViolation("text 必须非空")
        if not _normalized(self.text):
            raise ParsingContractViolation("text 必须已 normalize whitespace")
        if not isinstance(self.text_sha256, str) or len(self.text_sha256) != 64:
            raise ParsingContractViolation("text_sha256 必须是 64 位 hex")
        if self.text_sha256 != _sha256_hex(self.text):
            raise ParsingContractViolation("text_sha256 必须等于 text 的 SHA-256")
        if not isinstance(self.locator, dict):
            raise ParsingContractViolation("locator 必须是 dict")
        locator_type = self.locator.get("type")
        expected_keys = _LOCATOR_SPECS.get(locator_type)
        if expected_keys is None:
            raise ParsingContractViolation("locator.type 必须匹配已知定位类型")
        if set(self.locator) != expected_keys:
            raise ParsingContractViolation("locator 字段必须精确匹配")
        if locator_type == "html_dom":
            self._validate_html_locator()
        else:
            self._validate_pdf_locator()

    def _validate_html_locator(self) -> None:
        if self.locator.get("ordinal") != self.ordinal:
            raise ParsingContractViolation("locator.ordinal 必须等于 block.ordinal")
        tag = self.locator.get("tag")
        xpath = self.locator.get("xpath")
        element_id = self.locator.get("element_id")
        if not isinstance(tag, str) or not tag:
            raise ParsingContractViolation("locator.tag 必须是非空字符串")
        if not isinstance(xpath, str) or not xpath.startswith("/"):
            raise ParsingContractViolation("locator.xpath 必须是绝对 xpath")
        if element_id is not None and not isinstance(element_id, str):
            raise ParsingContractViolation("locator.element_id 必须是字符串或 None")

    def _validate_pdf_locator(self) -> None:
        page_number = self.locator.get("page_number")
        line_index = self.locator.get("line_index")
        bbox = self.locator.get("bbox")
        page_width = self.locator.get("page_width")
        page_height = self.locator.get("page_height")
        for name, value in (("page_number", page_number), ("line_index", line_index)):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ParsingContractViolation(f"locator.{name} 必须是 >= 1 的 int")
        for name, value in (("page_width", page_width), ("page_height", page_height)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ParsingContractViolation(f"locator.{name} 必须是正数")
        if not isinstance(bbox, list) or len(bbox) != 4:
            raise ParsingContractViolation("locator.bbox 必须是 [x0, top, x1, bottom]")
        for value in bbox:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ParsingContractViolation("locator.bbox 分量必须是数字")
        x0, top, x1, bottom = bbox
        if not (0.0 <= x0 <= x1 <= float(page_width)):
            raise ParsingContractViolation("locator.bbox 必须在 page 宽度范围内")
        if not (0.0 <= top <= bottom <= float(page_height)):
            raise ParsingContractViolation("locator.bbox 必须在 page 高度范围内")


@dataclass(frozen=True)
class ParsedDocument:
    """HTML Parser 的确定性输出（不含 DB ID / 解析时间戳）。"""

    parser_name: str
    parser_version: int
    raw_content_sha256: str
    extracted_title: str | None
    extracted_published_at: datetime | None
    blocks: tuple[ParsedBlock, ...]

    def __post_init__(self) -> None:
        if _parser_specs().get(self.parser_name) != self.parser_version:
            raise ParsingContractViolation("parser_name/version 必须匹配已注册的解析器")
        if not isinstance(self.raw_content_sha256, str) or len(self.raw_content_sha256) != 64:
            raise ParsingContractViolation("raw_content_sha256 必须是 64 位 hex")
        if self.extracted_title is not None:
            if not isinstance(self.extracted_title, str) or not self.extracted_title.strip():
                raise ParsingContractViolation("extracted_title 必须非空或 None")
            if not _normalized(self.extracted_title):
                raise ParsingContractViolation("extracted_title 必须已 normalize")
        if self.extracted_published_at is not None:
            published = self.extracted_published_at
            if (
                not isinstance(published, datetime)
                or published.tzinfo is None
                or published.utcoffset() is None
            ):
                raise ParsingContractViolation(
                    "extracted_published_at 必须是 timezone-aware datetime"
                )
        for index, block in enumerate(self.blocks, start=1):
            if block.ordinal != index:
                raise ParsingContractViolation("blocks 的 ordinal 必须连续 1..n")


def compute_parse_fingerprint(source_id: UUID, document: ParsedDocument) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：source_id、raw artifact sha256、parser_name/version、
    extracted metadata、ordered blocks（text + locator）。**不得包含**
    parsed_at / created_at / DB ID，禁止 repr() / hash() 序列化。

    同一 Source + 相同 raw bytes + parser version → 同一指纹。
    """
    payload = {
        "source_id": str(source_id),
        "raw_content_sha256": document.raw_content_sha256,
        "parser_name": document.parser_name,
        "parser_version": document.parser_version,
        "extracted_title": document.extracted_title,
        "extracted_published_at": (
            document.extracted_published_at.isoformat()
            if document.extracted_published_at is not None
            else None
        ),
        "blocks": [
            {
                "ordinal": block.ordinal,
                "block_type": block.block_type.value,
                "text": block.text,
                "text_sha256": block.text_sha256,
                "locator": block.locator,
            }
            for block in document.blocks
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
