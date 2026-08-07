"""Deterministic parsing contracts (stage 2E.1).

ParsedBlock / ParsedDocument 是 HTML Parser 的确定性输出契约：

- ParsedDocument 是"某一 SourceRecord + 特定 parser 版本下的解析快照"
  （不含 DB ID、不含 parsed_at/created_at），后续由 SourceParsingService
  计算 parse_fingerprint 并落库；
- ParsedBlock 带 locator（DOM 级定位），供后续 Evidence 原文核对。

ParsedSource 是 SourceRecord 的确定性解析快照，不是 Chunk，也不是 Evidence。
"""

import hashlib
import itertools
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

_LOCATOR_TYPE = "html_dom"
_LOCATOR_KEYS = {"type", "ordinal", "tag", "xpath", "element_id"}


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

    locator 是 DOM 级定位（type/ordinal/tag/xpath/element_id），同一
    raw bytes + parser version 下完全稳定；不含绝对路径 / 浏览器坐标。
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
        if set(self.locator) != _LOCATOR_KEYS:
            raise ParsingContractViolation("locator 字段必须精确匹配五键")
        if self.locator.get("type") != _LOCATOR_TYPE:
            raise ParsingContractViolation("locator.type 必须是 html_dom")
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
        if self.parser_name != HTML_PARSER_NAME:
            raise ParsingContractViolation("parser_name 必须匹配 HTML_PARSER_NAME")
        if self.parser_version != HTML_PARSER_VERSION:
            raise ParsingContractViolation("parser_version 必须匹配 HTML_PARSER_VERSION")
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
        for left, right in itertools.pairwise(self.blocks):
            if (left.block_type, left.text) == (right.block_type, right.text):
                raise ParsingContractViolation("相邻 block 不得完全相同")


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
