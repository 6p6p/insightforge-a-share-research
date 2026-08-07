"""Minimal deterministic PDF fixture builder (stage 2E.2).

手写极小的合法 PDF（纯 stdlib，零第三方依赖，不联网）：只生成"机器可读
PDF"——Type1 Helvetica 文本、多页、空页、重复字符、CJK（Type0 Identity-H +
ToUnicode 恒等映射）、metadata Title、加密 trailer（V=1/R=2，密码错误路径）。
测试用本模块生成 bytes 后经 LocalRawArtifactStore.put_pdf_stream 归档，再走
真实 SourceParsingService；不引入 reportlab/fpdf 等 PDF 生成运行时依赖。

重复文本语义（PDF v2 收口）：
- 相同位置重复绘制字符（同一坐标两次 'aa'）→ 由 pdfplumber dedupe_chars 去重；
- 不同位置的相同文本行（同页不同 bbox / 跨页）→ 全部保留，各自独立 block，
  由 pdf_page locator（page_number/line_index/bbox）区分原文位置。

坐标语义：spans 里 (x, y) 是 PDF 坐标（原点在页面左下角，单位 pt）；
pdfplumber 的 top 从页面顶部算起，所以 top = page_height - y - 字形高度。
"""

from __future__ import annotations

PAGE_WIDTH = 612.0
PAGE_HEIGHT = 792.0


def _esc(text: str) -> str:
    return text.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")


def _info_string(text: str) -> str:
    """PDF Info 字符串：纯 ASCII 用字面量；否则 UTF-16BE(BOM) 十六进制串。

    pdfminer 的 decode_text 遇到 <FEFF...> 按 UTF-16BE 解码，可承载中文
    metadata Title。
    """
    if all(ord(c) < 128 for c in text):
        return f"({_esc(text)})"
    hexs = "FEFF" + "".join(f"{ord(c):04X}" for c in text)
    return f"<{hexs}>"


class _Alloc:
    def __init__(self) -> None:
        self._n = 0

    def next(self) -> int:
        self._n += 1
        return self._n


def build_pdf(
    pages: list[list[tuple[float, float, str]]],
    *,
    title: str | None = None,
    encrypted: bool = False,
    cjk: bool = False,
) -> bytes:
    """按给定页（每页是 (x, y, text) 序列）构建确定性最小 PDF bytes。

    - cjk=True 时文本用 Type0 Identity-H 字体 + UTF-16BE 十六进制字符串 +
      ToUnicode 恒等映射（CID == Unicode code point），pdfminer 可提取中文。
    - encrypted=True 时 trailer 携带标准 V=1/R=2 Encrypt 字典，pdfminer
      无密码读取会抛 PDFPasswordIncorrect。
    """
    alloc = _Alloc()
    objs: dict[int, str] = {}

    def add(body: str) -> int:
        n = alloc.next()
        objs[n] = body
        return n

    catalog = add("<< /Type /Catalog /Pages 0 0 R >>")
    pages_n = add("")  # 占位，之后填
    page_infos: list[tuple[int, int]] = []
    for spans in pages:
        parts = ["BT /F1 12 Tf"]
        for x, y, text in spans:
            parts.append(f"1 0 0 1 {x:.2f} {y:.2f} Tm")
            if cjk:
                hexs = " ".join(f"{ord(c):04X}" for c in text)
                parts.append(f"<{hexs}> Tj")
            else:
                parts.append(f"({_esc(text)}) Tj")
        parts.append("ET")
        stream = "\n".join(parts)
        ct = add(f"<< /Length {len(stream.encode('latin-1'))} >>\nstream\n{stream}\nendstream")
        pg = add("")
        page_infos.append((pg, ct))

    font_main = add("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    if cjk:
        cid = add(
            "<< /Type /Font /Subtype /CIDFontType0 /BaseFont /TestCJK "
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>"
        )
        cmap = (
            "/CIDInit /ProcSet findresource begin\n"
            "12 dict begin\nbegincmap\n"
            "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def\n"
            "/CMapName /Adobe-Identity-UCS def\n/CMapType 2 def\n"
            "1 begincodespacerange\n<0000> <FFFF>\nendcodespacerange\n"
            "1 beginbfrange\n<0000> <FFFF> <0000>\nendbfrange\n"
            "endcmap\nCMapName currentdict /CMap defineresource pop\nend\nend"
        )
        touni = add(f"<< /Length {len(cmap.encode('latin-1'))} >>\nstream\n{cmap}\nendstream")
        font_main = add(
            "<< /Type /Font /Subtype /Type0 /BaseFont /TestCJK "
            f"/Encoding /Identity-H /DescendantFonts [{cid} 0 R] /ToUnicode {touni} 0 R >>"
        )

    kids = []
    for pg, ct in page_infos:
        kids.append(f"{pg} 0 R")
        objs[pg] = (
            f"<< /Type /Page /Parent {pages_n} 0 R "
            f"/MediaBox [0 0 {PAGE_WIDTH:.0f} {PAGE_HEIGHT:.0f}] "
            f"/Resources << /Font << /F1 {font_main} 0 R >> >> /Contents {ct} 0 R >>"
        )
    objs[pages_n] = f"<< /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >>"
    objs[catalog] = f"<< /Type /Catalog /Pages {pages_n} 0 R >>"

    info_n = enc_n = None
    if title:
        info_n = add(f"<< /Title {_info_string(title)} >>")
    if encrypted:
        enc_n = add(
            "<< /Filter /Standard /V 1 /R 2 "
            "/O <0123456789abcdef0123456789abcdef> "
            "/U <fedcba9876543210fedcba9876543210> /P -4 /Length 40 >>"
        )

    out = [b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n"]
    offsets: dict[int, int] = {}
    for n in sorted(objs):
        offsets[n] = len(b"".join(out))
        out.append(f"{n} 0 obj\n{objs[n]}\nendobj\n".encode("latin-1"))
    xref_off = len(b"".join(out))
    count = max(offsets) + 1
    xref = [f"xref\n0 {count}\n", "0000000000 65535 f \n"]
    for n in range(1, count):
        xref.append(f"{offsets[n]:010d} 00000 n \n")
    trailer = (
        f"trailer\n<< /Size {count} /Root {catalog} 0 R "
        + (f"/Info {info_n} 0 R " if info_n else "")
        + (f"/Encrypt {enc_n} 0 R " if enc_n else "")
        + ">>\n"
    )
    out.append("".join(xref).encode("latin-1"))
    out.append(trailer.encode("latin-1"))
    out.append(f"startxref\n{xref_off}\n%%EOF\n".encode("latin-1"))
    return b"".join(out)


def single_page_pdf(*, title: str | None = None) -> bytes:
    """单页、三行 ASCII（可带 metadata Title）。"""
    return build_pdf(
        [
            [
                (72.0, 720.0, "Hello world"),
                (72.0, 700.0, "Second line"),
                (72.0, 680.0, "Third line"),
            ]
        ],
        title=title,
    )


def multi_page_pdf() -> bytes:
    """两页，各自一行文本，用于 page_number 顺序 / line_index 重置。"""
    return build_pdf([[(72.0, 720.0, "Page one")], [(72.0, 700.0, "Page two")]])


def empty_page_then_text_pdf() -> bytes:
    """第一页空（无文本），第二页有文本：单页无文字不失败。"""
    return build_pdf([[], [(72.0, 720.0, "Has text")]])


def chinese_pdf() -> bytes:
    """中文 PDF（Type0 Identity-H + ToUnicode）。"""
    return build_pdf([[(72.0, 720.0, "中文段落：确定性解析。")]], cjk=True)


def duplicate_chars_pdf() -> bytes:
    """同一位置绘制两次 'aa'：字符重叠需 dedupe_chars 去重。"""
    return build_pdf([[(72.0, 720.0, "aa"), (72.0, 720.0, "aa")]])


def duplicate_line_same_page_pdf() -> bytes:
    """同页两个不同 bbox 的 'Dup'（不同 top）：位置不同 → 两个独立 block。

    PDF v2 收口：原文不同位置的相同文本必须全部保留，不按文本内容去重。
    """
    return build_pdf([[(72.0, 720.0, "Dup"), (72.0, 700.0, "Dup")]])


def duplicate_line_across_pages_pdf() -> bytes:
    """page1 与 page2 各一个 'Dup'：跨页相同文本 → 两个独立 block（page 不同）。

    PDF v2 收口：跨页相同文本行不按文本去重，由 page_number locator 区分。
    """
    return build_pdf(
        [
            [(72.0, 720.0, "Header"), (72.0, 700.0, "Dup")],
            [(72.0, 720.0, "Dup"), (72.0, 700.0, "Body two")],
        ]
    )


def encrypted_pdf() -> bytes:
    """加密 PDF（无密码不可读）。"""
    return build_pdf([[(72.0, 720.0, "Secret")]], encrypted=True)
