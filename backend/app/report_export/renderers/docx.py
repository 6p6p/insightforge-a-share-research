"""DOCX renderer (stage 6C spec O)：python-docx，纯函数，不查 DB。

结构：标题 + 元数据 + section（Heading1）+ 段落（正文 + [n] 标记）+ 证据附录
（E1..En）+ audit_note。段落 `text` 原样（**不改写句子**），编号标记由
citation_numbers 追加。输出为内存 bytes（reopen-able）。

**字节确定性**：显式写死 core properties（created/modified 用固定值），
避免默认模板的时间戳导致两次渲染字节不同（verify_export_integrity 按
content_sha256 比对归档字节）。
"""

from datetime import datetime
from io import BytesIO
from zoneinfo import ZoneInfo

from docx import Document
from docx.oxml.ns import qn
from docx.shared import Pt

from app.report_export.contracts import ExportReportPack

# 固定 core properties（字节确定性；与真实创建时间无关）。
_FIXED_CREATED = datetime(1970, 1, 1, tzinfo=ZoneInfo("UTC"))

# 中文正文西文字体 + East Asian 字体（Word 渲染时按名称解析，text 提取不受影响）。
_LATIN_FONT = "Calibri"
_EAST_ASIAN_FONT = "宋体"

_DATE_FMT = "%Y-%m-%d"


def render_docx(pack: ExportReportPack) -> bytes:
    document = Document()

    cp = document.core_properties
    cp.title = f"{pack.company_name} 基本面研究报告"
    cp.subject = "证据驱动基本面研究（InsightForge）"
    cp.author = "InsightForge"
    cp.created = _FIXED_CREATED
    cp.modified = _FIXED_CREATED

    _set_default_east_asian(document)

    security = f"（{pack.security_code}）" if pack.security_code else ""
    document.add_heading(f"{pack.company_name} 基本面研究报告{security}", level=0)
    _add_meta(document, "研究问题", pack.research_question or "—")
    _add_meta(document, "分析基准日", pack.analysis_as_of.isoformat())

    for section in pack.sections:
        document.add_heading(section.title, level=1)
        for paragraph in section.paragraphs:
            markers = "".join(f"[{n}]" for n in paragraph.citation_numbers)
            _add_body(document, f"{paragraph.text}{markers}")

    if pack.citations:
        document.add_heading("证据附录", level=1)
        for citation in pack.citations:
            document.add_heading(f"E{citation.number} ｜ {citation.provider_label or '—'}", level=2)
            _add_citation_lines(document, citation)

    if pack.audit_note:
        paragraph = document.add_paragraph(pack.audit_note)
        paragraph.style = document.styles["Quote"]

    buffer = BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _set_default_east_asian(document: Document) -> None:
    """Normal 样式同时设置西文 + East Asian 字体（中文正文可读）。"""
    normal = document.styles["Normal"]
    normal.font.name = _LATIN_FONT
    normal.font.size = Pt(11)
    rpr = normal.element.get_or_add_rPr()
    rfonts = rpr.get_or_add_rFonts()
    rfonts.set(qn("w:eastAsia"), _EAST_ASIAN_FONT)


def _add_meta(document: Document, label: str, value: str) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run(f"{label}：")
    run.bold = True
    paragraph.add_run(value)


def _add_body(document: Document, text: str) -> None:
    paragraph = document.add_paragraph(text)
    paragraph.paragraph_format.first_line_indent = Pt(22)


def _add_citation_lines(document: Document, citation) -> None:
    _add_bullet(document, "证据陈述", citation.statement or "—")
    if citation.quote_text:
        _add_bullet(document, "引用原文", f"「{citation.quote_text}」")
    _add_bullet(
        document, "提供方", f"{citation.provider_label or '—'}（{citation.provider_key or '—'}）"
    )
    _add_bullet(document, "来源", citation.title or "—")
    if citation.source_url:
        _add_bullet(document, "原始网页", citation.source_url)
    if citation.published_at is not None:
        _add_bullet(document, "发布", citation.published_at.strftime(_DATE_FMT))
    if citation.fetched_at is not None:
        _add_bullet(document, "获取", citation.fetched_at.strftime(_DATE_FMT))
    if citation.page_number is not None:
        _add_bullet(document, "定位", f"第 {citation.page_number} 页")
    if citation.xpath:
        _add_bullet(document, "定位", citation.xpath)
    if (
        citation.indicator is not None
        or citation.geography is not None
        or citation.period is not None
    ):
        parts = []
        if citation.indicator:
            parts.append(f"指标 {citation.indicator}")
        if citation.geography:
            parts.append(f"地域 {citation.geography}")
        if citation.period:
            parts.append(f"观测期 {citation.period}")
        _add_bullet(document, "宏观", " ｜ ".join(parts))


def _add_bullet(document: Document, label: str, value: str) -> None:
    paragraph = document.add_paragraph()
    run = paragraph.add_run(f"{label}：")
    run.bold = True
    paragraph.add_run(value)
    paragraph.paragraph_format.left_indent = Pt(12)
