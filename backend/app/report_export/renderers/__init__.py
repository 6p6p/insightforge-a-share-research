"""Export renderer registry (stage 6C spec O)：纯函数，不查 DB。

`render_export(pack, format)` → bytes。renderer 只消费 `ExportReportPack`，
不重写正文、不生成观点、不判断证据、不调用 LLM / Retrieval / Chroma / Web。
三个 renderer 均字节确定性（同 pack → 同 bytes）。
"""

from app.report_export.contracts import (
    EXPORT_FORMAT_DOCX,
    EXPORT_FORMAT_MARKDOWN,
    EXPORT_FORMAT_PDF,
    ExportReportPack,
)
from app.report_export.errors import ReportExportError
from app.report_export.renderers.docx import render_docx
from app.report_export.renderers.markdown import render_markdown
from app.report_export.renderers.pdf import render_pdf

_RENDERERS = {
    EXPORT_FORMAT_MARKDOWN: render_markdown,
    EXPORT_FORMAT_DOCX: render_docx,
    EXPORT_FORMAT_PDF: render_pdf,
}


def render_export(pack: ExportReportPack, format: str) -> bytes:
    renderer = _RENDERERS.get(format)
    if renderer is None:
        raise ReportExportError(f"不支持的导出格式: {format}")
    return renderer(pack)
