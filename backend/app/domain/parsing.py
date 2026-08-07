"""Parsing domain enums (stage 2E.1).

ParsedSource 是 SourceRecord 的确定性解析快照，不是 Chunk，也不是 Evidence。
本模块只定义通用解析契约（媒体无关：PDF 后续复用同一 ParsedSource/Block 模型，
只是 parser_name / locator 不同）。

ParsedBlockType 冻结为五类文本块：heading / paragraph / list_item /
blockquote / table_text。HTML 与未来 PDF 解析都归入这些类型，不按来源细分。
"""

from enum import StrEnum


class ParsedBlockType(StrEnum):
    """ParsedSourceBlock.block_type 冻结的五类（2E.1）。

    heading     → h1-h6 / PDF 章节标题
    paragraph   → <p> / PDF 正文段落
    list_item   → <li>
    blockquote  → <blockquote>
    table_text  → <table> 内单元格文本
    """

    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    BLOCKQUOTE = "blockquote"
    TABLE_TEXT = "table_text"
