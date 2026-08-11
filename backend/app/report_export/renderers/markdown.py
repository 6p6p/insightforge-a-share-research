"""Markdown renderer (stage 6C spec O)：UTF-8 字节输出，纯函数，不查 DB。

正文段落 `text` 原样输出 + 末尾追加 `[n]` 编号标记（n = citation_numbers
顺序），**绝不改写句子 / 不重排 / 不重新编号**。附录 E1..En 提供证据 / 来源 /
定位详情。audit_note（人工批准路径）以引用块收尾。
"""

from app.report_export.contracts import ExportReportPack

# 时间字段展示模板（None → "—"）。
_DATE_FMT = "%Y-%m-%d"


def render_markdown(pack: ExportReportPack) -> bytes:
    lines: list[str] = []
    security = f"（{pack.security_code}）" if pack.security_code else ""
    lines.append(f"# {pack.company_name} 基本面研究报告{security}")
    lines.append("")
    lines.append("- 研究问题：" + (pack.research_question or "—"))
    lines.append("- 分析基准日：" + pack.analysis_as_of.isoformat())
    lines.append("")
    lines.append("---")
    lines.append("")

    for section in pack.sections:
        lines.append(f"## {section.title}")
        lines.append("")
        for paragraph in section.paragraphs:
            markers = "".join(f"[{n}]" for n in paragraph.citation_numbers)
            lines.append(f"{paragraph.text}{markers}")
            lines.append("")

    if pack.citations:
        lines.append("## 证据附录")
        lines.append("")
        for citation in pack.citations:
            lines.append(f"### E{citation.number} ｜ {citation.provider_label or '—'}")
            lines.append("")
            lines.append(f"- 证据陈述：{citation.statement or '—'}")
            if citation.quote_text:
                lines.append(f"- 引用原文：「{citation.quote_text}」")
            lines.append(
                f"- 提供方：{citation.provider_label or '—'}（{citation.provider_key or '—'}）"
            )
            lines.append(f"- 来源：{citation.title or '—'}")
            if citation.source_url:
                lines.append(f"- 原始网页：{citation.source_url}")
            if citation.published_at is not None:
                lines.append(f"- 发布：{citation.published_at.strftime(_DATE_FMT)}")
            if citation.fetched_at is not None:
                lines.append(f"- 获取：{citation.fetched_at.strftime(_DATE_FMT)}")
            if citation.page_number is not None:
                lines.append(f"- 定位：第 {citation.page_number} 页")
            if citation.xpath:
                lines.append(f"- 定位：{citation.xpath}")
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
                lines.append("- 宏观：" + " ｜ ".join(parts))
            lines.append("")

    if pack.audit_note:
        lines.append("> " + pack.audit_note)
        lines.append("")

    return "\n".join(lines).encode("utf-8")
