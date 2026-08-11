"""PDF renderer (stage 6C spec O)：reportlab + UnicodeCIDFont，纯函数，不查 DB。

中文用内置 CID 字体 `STSong-Light`（无需字体文件 → Windows 开发机与 Docker
字节一致，可被 pdfplumber 提取出真实中文）。

**字节确定性**：`SimpleDocTemplate(invariant=1)`——reportlab 的 invariant 模式
把 CreationDate / ModDate / document ID 固定为确定性值；title / author /
creator / subject / producer 显式写死。正文段落 `text` 原样 + `[n]` 编号标记，
**绝不改写句子**；分段/分页由 Platypus 确定性布局。
"""

from io import BytesIO

from reportlab.lib import colors
from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

from app.report_export.contracts import ExportReportPack

_CID_FONT = "STSong-Light"
_DATE_FMT = "%Y-%m-%d"

# 固定 document info（invariant 模式下仍显式写死，保证字节确定性）。
_PRODUCER = "InsightForge"

# 单行正文宽度上限（A4 减去边距后约 166mm；中文按字符计数）。
_BODY_MAX_CHARS = 34


def _register_font() -> None:
    if _CID_FONT not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(UnicodeCIDFont(_CID_FONT))


def render_pdf(pack: ExportReportPack) -> bytes:
    _register_font()
    buffer = BytesIO()

    document = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=22 * mm,
        rightMargin=22 * mm,
        topMargin=20 * mm,
        bottomMargin=20 * mm,
        title=f"{pack.company_name} 基本面研究报告",
        author="InsightForge",
        creator="InsightForge",
        subject="证据驱动基本面研究",
        producer=_PRODUCER,
        invariant=1,
    )

    styles = _build_styles()
    story: list = []

    security = f"（{pack.security_code}）" if pack.security_code else ""
    story.append(Paragraph(f"{pack.company_name} 基本面研究报告{security}", styles["title"]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph(f"研究问题：{pack.research_question or '—'}", styles["meta"]))
    story.append(Paragraph(f"分析基准日：{pack.analysis_as_of.isoformat()}", styles["meta"]))
    story.append(Spacer(1, 4 * mm))

    for section in pack.sections:
        story.append(Paragraph(section.title, styles["h1"]))
        story.append(Spacer(1, 2 * mm))
        for paragraph in section.paragraphs:
            markers = "".join(f"[{n}]" for n in paragraph.citation_numbers)
            story.append(Paragraph(_wrap_body(f"{paragraph.text}{markers}"), styles["body"]))
            story.append(Spacer(1, 2 * mm))

    if pack.citations:
        story.append(Spacer(1, 3 * mm))
        story.append(Paragraph("证据附录", styles["h1"]))
        story.append(Spacer(1, 2 * mm))
        for citation in pack.citations:
            story.append(
                Paragraph(f"E{citation.number} ｜ {citation.provider_label or '—'}", styles["h2"])
            )
            story.append(Paragraph(_citation_lines(citation), styles["body"]))
            story.append(Spacer(1, 3 * mm))

    if pack.audit_note:
        story.append(Paragraph(f"＞ {pack.audit_note}", styles["note"]))

    document.build(story)
    return buffer.getvalue()


def _build_styles():
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ExportTitleCN",
            parent=base["Title"],
            fontName=_CID_FONT,
            fontSize=18,
            leading=24,
            spaceAfter=2 * mm,
        ),
        "meta": ParagraphStyle(
            "ExportMetaCN",
            fontName=_CID_FONT,
            fontSize=10,
            leading=14,
            textColor=colors.HexColor("#444444"),
        ),
        "h1": ParagraphStyle(
            "ExportH1CN",
            parent=base["Heading1"],
            fontName=_CID_FONT,
            fontSize=14,
            leading=20,
            spaceBefore=3 * mm,
            spaceAfter=2 * mm,
        ),
        "h2": ParagraphStyle(
            "ExportH2CN",
            parent=base["Heading2"],
            fontName=_CID_FONT,
            fontSize=11.5,
            leading=16,
            spaceBefore=2 * mm,
            spaceAfter=1.5 * mm,
        ),
        "body": ParagraphStyle(
            "ExportBodyCN",
            fontName=_CID_FONT,
            fontSize=10.5,
            leading=16,
            alignment=TA_JUSTIFY,
            spaceAfter=2 * mm,
        ),
        "note": ParagraphStyle(
            "ExportNoteCN",
            parent=base["BodyText"],
            fontName=_CID_FONT,
            fontSize=10,
            leading=14,
            borderWidth=1,
            borderColor=colors.HexColor("#faad14"),
            borderPadding=6,
            spaceAfter=3 * mm,
        ),
    }


def _wrap_body(text: str) -> str:
    """把中文长行按字符宽度插入零宽空格换行（CID 字体按字符计量，不破坏词）。

    对中文字符每 `_BODY_MAX_CHARS` 字符插入一个空格；连续 ASCII 段保持原样。
    只影响 PDF 排版换行，**不改写句子内容**。
    """
    out: list[str] = []
    count = 0
    for ch in text:
        out.append(ch)
        count += 1
        if count >= _BODY_MAX_CHARS and _is_wide(ch):
            out.append("​")
            count = 0
    return "".join(out)


def _is_wide(ch: str) -> bool:
    return ord(ch) > 0x2E80


def _citation_lines(citation) -> str:
    lines = [f"证据陈述：{citation.statement or '—'}"]
    if citation.quote_text:
        lines.append(f"引用原文：「{citation.quote_text}」")
    lines.append(f"提供方：{citation.provider_label or '—'}（{citation.provider_key or '—'}）")
    lines.append(f"来源：{citation.title or '—'}")
    if citation.source_url:
        lines.append(f"原始网页：{citation.source_url}")
    if citation.published_at is not None:
        lines.append(f"发布：{citation.published_at.strftime(_DATE_FMT)}")
    if citation.fetched_at is not None:
        lines.append(f"获取：{citation.fetched_at.strftime(_DATE_FMT)}")
    if citation.page_number is not None:
        lines.append(f"定位：第 {citation.page_number} 页")
    if citation.xpath:
        lines.append(f"定位：{citation.xpath}")
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
        lines.append("宏观：" + " ｜ ".join(parts))
    return "<br/>".join(lines)
