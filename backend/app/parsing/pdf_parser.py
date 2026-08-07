"""Deterministic PDF parser (stage 2E.2).

用 pdfplumber（本阶段新增依赖，锁定 0.11.x）把已归档的 application/pdf
SourceRecord 原始字节确定性解析为可定位结构化文本：

- 只读 bytes（BytesIO），不联网、不写临时外部文件、不修改 PDF；
- 仅支持机器可读 PDF；**整个 PDF** 无任何可提取文本 → PdfTextUnavailable
  （OCR 留未来）；单页无文字不失败；
- 每页先 `page.dedupe_chars()`（去掉相同位置的重复绘制字符），再
  `extract_words(use_text_flow=False, keep_blank_chars=False,
  expand_ligatures=True, 固定 x/y tolerance)` 取稳定 word 级输出；
- 固定排序 page_number ASC → top ASC → x0 ASC；固定 y tolerance 聚合
  words 为行；每行一个 block（block_type=paragraph，不做 heading/语义推断）；
- **不做 text-level 去重**：原文不同位置的相同文本必须全部保留，由
  pdf_page locator（page_number/line_index/bbox）区分原文位置；
  `dedupe_chars` 只处理同一坐标的重复绘制字符，与文本内容去重职责不同；
  相同文本行（同页不同 bbox 或跨页）各自独立成 block（2E.2 收口，PDF v2）；
- locator = {"type":"pdf_page","page_number":N,"line_index":M,
  "bbox":[x0,top,x1,bottom],"page_width":...,"page_height":...}，
  全部 float round(...,3)；page_number/line_index 1-based；
- extracted_title 取 PDF metadata Title（normalize 后非空否则 None）；
  extracted_published_at 恒为 None（绝不使用 CreationDate/ModDate）；
- 安全边界：PDF magic 必须有效；加密/密码保护 → PdfEncryptedError；
  page_count 不在 1..1000 或提取字符总量超 5,000,000 → PdfResourceLimitError；
  非加密但损坏 → PdfParseError。
"""

import hashlib
from io import BytesIO

import pdfplumber
from pdfminer.pdfdocument import PDFEncryptionError, PDFPasswordIncorrect
from pdfminer.psexceptions import PSException
from pdfplumber.utils.exceptions import PdfminerException

from app.domain.parsing import ParsedBlockType
from app.parsing.contracts import (
    PDF_PARSER_NAME,
    PDF_PARSER_VERSION,
    ParsedBlock,
    ParsedDocument,
)
from app.parsing.errors import (
    PdfEncryptedError,
    PdfParseError,
    PdfResourceLimitError,
    PdfTextUnavailable,
)

# 资源限制（模块级常量，可被测试 monkeypatch）。
MAX_PDF_PAGE_COUNT = 1000
MAX_PDF_TOTAL_CHARS = 5_000_000

# 固定提取参数（确定性）：word 级 x/y tolerance 固定值。
_WORD_X_TOLERANCE = 2.0
_WORD_Y_TOLERANCE = 3.0
# 行聚合 y tolerance：word.top 与行引用 top 差值 ≤ 该值则并入同一行。
_LINE_Y_TOLERANCE = 3.0

_PDF_MAGIC = b"%PDF-"
# %PDF- 前只允许少量 ASCII 空白。
_PDF_LEADING_WS = b"\t\n\r\x20"


def parse_pdf_bytes(raw: bytes) -> ParsedDocument:
    """把归档的 PDF 原始字节解析为确定性 ParsedDocument。

    magic 无效 → PdfParseError；加密 → PdfEncryptedError；页数/字符超限 →
    PdfResourceLimitError；整个 PDF 无文本 → PdfTextUnavailable。
    """
    if not raw or not raw.lstrip(_PDF_LEADING_WS).startswith(_PDF_MAGIC):
        raise PdfParseError()
    collected: list[dict] = []
    metadata_title: object = None
    try:
        with pdfplumber.open(BytesIO(raw), password="") as pdf:
            page_count = len(pdf.pages)
            if page_count < 1 or page_count > MAX_PDF_PAGE_COUNT:
                raise PdfResourceLimitError()
            total_chars = 0
            metadata_title = (pdf.metadata or {}).get("Title")
            for page in pdf.pages:
                # dedupe_chars 返回新的 FilteredPage，必须重新赋值。
                page = page.dedupe_chars()
                words = page.extract_words(
                    use_text_flow=False,
                    keep_blank_chars=False,
                    expand_ligatures=True,
                    x_tolerance=_WORD_X_TOLERANCE,
                    y_tolerance=_WORD_Y_TOLERANCE,
                )
                for line in _group_words_into_lines(words, _LINE_Y_TOLERANCE):
                    text = _normalize(line["text"])
                    if not text:
                        continue
                    total_chars += len(text)
                    if total_chars > MAX_PDF_TOTAL_CHARS:
                        raise PdfResourceLimitError()
                    collected.append(
                        {
                            "page_number": page.page_number,
                            "page_width": page.width,
                            "page_height": page.height,
                            "line": line,
                            "text": text,
                        }
                    )
    except PdfminerException as exc:
        cause = exc.args[0] if exc.args else None
        if isinstance(cause, (PDFEncryptionError, PDFPasswordIncorrect)):
            raise PdfEncryptedError() from exc
        raise PdfParseError() from exc
    except (PDFEncryptionError, PDFPasswordIncorrect) as exc:
        raise PdfEncryptedError() from exc
    except PSException as exc:
        raise PdfParseError() from exc

    if not collected:
        raise PdfTextUnavailable()

    blocks: list[ParsedBlock] = []
    ordinal = 0
    line_counts: dict[int, int] = {}
    for item in collected:
        page_number = item["page_number"]
        line_counts[page_number] = line_counts.get(page_number, 0) + 1
        ordinal += 1
        blocks.append(
            _make_block(
                ordinal=ordinal,
                line_index=line_counts[page_number],
                text=item["text"],
                line=item["line"],
                page_width=item["page_width"],
                page_height=item["page_height"],
                page_number=page_number,
            )
        )
    return ParsedDocument(
        parser_name=PDF_PARSER_NAME,
        parser_version=PDF_PARSER_VERSION,
        raw_content_sha256=hashlib.sha256(raw).hexdigest(),
        extracted_title=_extract_title(metadata_title),
        extracted_published_at=None,
        blocks=tuple(blocks),
    )


def _group_words_into_lines(words: list[dict], y_tolerance: float) -> list[dict]:
    """固定 y tolerance 聚合 words 为行；行内按 x0 ASC，行间按 top ASC。

    words 先按 (top, x0) ASC 排序；行引用 top 取行内首个 word 的 top
    （后续 word 与行引用 top 差值 ≤ tolerance 则并入该行）。确定性：同一
    输入 words + 同一 tolerance → 同一行划分；每行产出 text 与 bbox 分量。
    """
    ordered = sorted(words, key=lambda w: (w["top"], w["x0"]))
    lines: list[dict] = []
    for word in ordered:
        if not lines or abs(word["top"] - lines[-1]["_top"]) > y_tolerance:
            lines.append({"_top": word["top"], "words": [word]})
        else:
            lines[-1]["words"].append(word)
    result: list[dict] = []
    for line in lines:
        ws = sorted(line["words"], key=lambda w: w["x0"])
        result.append(
            {
                "text": " ".join(w["text"] for w in ws),
                "x0": min(w["x0"] for w in ws),
                "top": min(w["top"] for w in ws),
                "x1": max(w["x1"] for w in ws),
                "bottom": max(w["bottom"] for w in ws),
            }
        )
    return result


def _make_block(
    *,
    ordinal: int,
    line_index: int,
    text: str,
    line: dict,
    page_width: float,
    page_height: float,
    page_number: int,
) -> ParsedBlock:
    """构造 pdf_page locator：全部 float round(...,3)，page/line 均 1-based。"""
    bbox = [
        round(float(line["x0"]), 3),
        round(float(line["top"]), 3),
        round(float(line["x1"]), 3),
        round(float(line["bottom"]), 3),
    ]
    return ParsedBlock(
        ordinal=ordinal,
        block_type=ParsedBlockType.PARAGRAPH,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        locator={
            "type": "pdf_page",
            "page_number": page_number,
            "line_index": line_index,
            "bbox": bbox,
            "page_width": round(float(page_width), 3),
            "page_height": round(float(page_height), 3),
        },
    )


def _extract_title(title: object) -> str | None:
    """PDF metadata Title，normalize 后非空否则 None；published_at 恒 None。"""
    if isinstance(title, bytes):
        title = title.decode("utf-8", errors="replace")
    if not isinstance(title, str):
        return None
    normalized = _normalize(title)
    return normalized or None


def _normalize(text: str) -> str:
    return " ".join(text.split())
