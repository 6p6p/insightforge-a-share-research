"""Deterministic HTML parser (stage 2E.1).

基于 lxml（本阶段唯一新增依赖）。只把已归档的 text/html SourceRecord
原始字节确定性解析为可定位结构化文本：

- 不联网、不执行 JS、不修改 RawArtifact；bytes 先按真实 meta 声明
  （<meta charset> / http-equiv Content-Type charset）确定性解码为 str，
  再交 lxml（绝不从 body/script 文本推断编码）；
- 删除 script/style/noscript/template/svg；
- 内容根优先 article → main → body；
- DOM 顺序抽取 h1-h6/p/li/blockquote/table；
- normalize 连续 whitespace；空文本跳过；相邻完全相同 block 去重；
- title 优先 og:title → <title> → h1 → None；
- published_at 只接受明确 publication 元数据：article:published_time meta
  或 [itemprop="datePublished"]（meta content / time datetime）；普通
  <time datetime> 无 itemprop 忽略；naive 时间无法可靠确定绝对时刻 → None；
  绝不使用 Candidate.seen_at / parsed_at / 当前时间伪造；
- 同一 raw bytes + parser version → 输出完全确定。
"""

import codecs
import hashlib
import re
from datetime import datetime
from html.parser import HTMLParser

from lxml import etree
from lxml import html as lxml_html

from app.domain.parsing import ParsedBlockType
from app.parsing.contracts import (
    HTML_PARSER_NAME,
    HTML_PARSER_VERSION,
    ParsedBlock,
    ParsedDocument,
)
from app.parsing.errors import HtmlParseError

_REMOVE_TAGS = ("script", "style", "noscript", "template", "svg")
_CONTAINER_TAGS = ("li", "blockquote", "table")
_HEADING_TAGS = frozenset(("h1", "h2", "h3", "h4", "h5", "h6"))
_BLOCK_TAGS = _HEADING_TAGS | frozenset(("p", "li", "blockquote", "table"))
_META_PROPERTIES = ("property", "name")
_CONTENT_ROOT_PREFERENCE = ("article", "main")

# 模块级共享 parser：不联网、recover 容忍坏 HTML、剔除注释/PI。
# 必须用 lxml.html.HTMLParser（etree.HTMLParser 不设置 HtmlElement 元素类，
# document_fromstring 会返回无 drop_tree/text_content 的 _Element）。
# 编码处理策略：不把 encoding 参数写死（libxml2 显式 encoding 会覆盖
# meta charset 声明），而是先 _detect_encoding（BOM → meta charset →
# UTF-8 默认）把 bytes 确定性解码为 str，再交 lxml（str 输入不再猜编码）。
_HTML_PARSER = lxml_html.HTMLParser(
    no_network=True,
    recover=True,
    remove_comments=True,
    remove_pis=True,
)

_BOM_UTF8 = codecs.BOM_UTF8
_HEAD_SCAN_BYTES = 8192
# 只认可 http-equiv="Content-Type" 的 content 值里 charset 参数（仅这一种
# 参数形式；http-equiv 大小写不敏感）。绝不从 <meta name="description"
# content="... charset=gbk"> 这类非 http-equiv 的 content 文本推断编码，
# 也不全局扫描任意 charset= 文本（body/script 内出现 charset=... 不影响）。
_CHARSET_PARAM_RE = re.compile(
    r"\bcharset\s*=\s*[\"']?([a-zA-Z0-9._\-]+)",
    re.IGNORECASE,
)


class _MetaCharsetParser(HTMLParser):
    """确定性 meta 编码声明扫描器（stdlib HTMLParser，纯本地不联网）。

    只认可两种真实 meta 声明：
      (a) <meta charset="gbk">（HTML5 属性形式，charset 属性本身）；
      (b) <meta http-equiv="Content-Type" content="text/html; charset=gbk">
          （http-equiv 大小写不敏感；charset 仅取自 Content-Type 的
          content 值内 charset 参数）。
    其余任何 attribute 值里出现的 charset= 文本（如
    <meta name="description" content="report charset=gbk">）都不产生声明。
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._declared: str | None = None

    @property
    def declared(self) -> str | None:
        return self._declared

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if self._declared is not None or tag.lower() != "meta":
            return
        attr_map = {key.lower(): value for key, value in attrs if value is not None}
        charset_attr = attr_map.get("charset")
        if charset_attr:
            self._set(charset_attr)
            return
        http_equiv = attr_map.get("http-equiv")
        content = attr_map.get("content")
        if http_equiv and http_equiv.strip().lower() == "content-type" and content:
            match = _CHARSET_PARAM_RE.search(content)
            if match:
                self._set(match.group(1))

    def _set(self, name: str) -> None:
        normalized = name.strip().lower()
        if normalized:
            self._declared = normalized


def parse_html_bytes(raw: bytes) -> ParsedDocument:
    """把归档的 HTML 原始字节解析为确定性 ParsedDocument。

    空字节 / 纯空白视为无法解析 → HtmlParseError；非空但内容为空白的页面
    返回 0 blocks 的合法 ParsedDocument。
    """
    if not raw or not raw.strip():
        raise HtmlParseError()
    text = _decode(raw)
    root = _load_dom(text)
    _strip_noise(root)
    content_root = _content_root(root)
    blocks = _extract_blocks(content_root)
    return ParsedDocument(
        parser_name=HTML_PARSER_NAME,
        parser_version=HTML_PARSER_VERSION,
        raw_content_sha256=hashlib.sha256(raw).hexdigest(),
        extracted_title=_extract_title(root, content_root),
        extracted_published_at=_extract_published_at(root),
        blocks=tuple(blocks),
    )


def _decode(raw: bytes) -> str:
    """确定性编码探测 + 解码：BOM → meta charset 声明 → UTF-8 默认。

    - 声明的编码不可用（LookupError / UnicodeDecodeError）时回退 UTF-8；
    - 无声明且非合法 UTF-8（损坏内容）→ HtmlParseError，不静默 latin-1
      乱码（证据链解析宁可失败，不可产出乱码文本）。
    """
    declared = _detect_encoding(raw)
    if declared is not None:
        try:
            return raw.decode(declared)
        except (LookupError, UnicodeDecodeError):
            pass
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise HtmlParseError() from exc


def _detect_encoding(raw: bytes) -> str | None:
    """只做纯本地字节扫描，不联网；返回声明编码或 None（→ 默认 UTF-8）。

    用 stdlib html.parser.HTMLParser 确定性扫描 head 字节的 <meta> 声明
    （latin-1 无损解码字节为 str：仅 ASCII 编码名会被提取，中文字节不受影响）。
    只从真实 meta 声明识别编码（<meta charset> 属性形式，或
    <meta http-equiv="Content-Type" content="...; charset=...">），
    绝不从 body/script 文本或任意 charset= 文本推断编码。
    """
    if raw.startswith(_BOM_UTF8):
        return "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return "utf-16"
    head = raw[:_HEAD_SCAN_BYTES].decode("latin-1")
    parser = _MetaCharsetParser()
    try:
        parser.feed(head)
        parser.close()
    except (ValueError, AssertionError, IndexError):
        # 畸形标记防御：解析失败即视为无声明（→ 默认 UTF-8），保持确定性。
        return None
    return parser.declared


def _load_dom(text: str) -> lxml_html.HtmlElement:
    # str 输入：lxml 直接使用已解码的 Unicode，不再涉及编码猜测。
    try:
        return lxml_html.document_fromstring(text, parser=_HTML_PARSER)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError, TypeError) as exc:
        raise HtmlParseError() from exc


def _strip_noise(root: lxml_html.HtmlElement) -> None:
    targets: list = []
    for tag in _REMOVE_TAGS:
        targets.extend(root.iter(tag))
    for element in targets:
        element.drop_tree()


def _content_root(root: lxml_html.HtmlElement) -> lxml_html.HtmlElement:
    for tag in _CONTENT_ROOT_PREFERENCE:
        candidates = root.xpath(f"//{tag}")
        if candidates:
            return candidates[0]
    body = root.find(".//body")
    return body if body is not None else root


def _extract_title(
    root: lxml_html.HtmlElement,
    content_root: lxml_html.HtmlElement,
) -> str | None:
    for attr in _META_PROPERTIES:
        for content in root.xpath(f"//meta[@{attr}='og:title']/@content"):
            normalized = _normalize(content)
            if normalized:
                return normalized
    for text in root.xpath("//title/text()"):
        normalized = _normalize(text)
        if normalized:
            return normalized
    h1 = content_root.xpath(".//h1")
    if h1:
        normalized = _normalize(h1[0].text_content())
        if normalized:
            return normalized
    return None


def _extract_published_at(root: lxml_html.HtmlElement) -> datetime | None:
    # 只认可明确 publication 元数据：
    #   1. <meta property/name="article:published_time" content="...">
    #   2. [itemprop="datePublished"]（<meta content> 或 <time datetime>）
    # 普通 <time datetime> 无 itemprop=datePublished → 忽略；updated_time /
    # dateModified 不冒充 published_at。
    for attr in _META_PROPERTIES:
        for content in root.xpath(f"//meta[@{attr}='article:published_time']/@content"):
            parsed = _parse_machine_datetime(content)
            if parsed is not None:
                return parsed
    for value in root.xpath(
        "//meta[@itemprop='datePublished']/@content | //time[@itemprop='datePublished']/@datetime"
    ):
        parsed = _parse_machine_datetime(value)
        if parsed is not None:
            return parsed
    return None


def _parse_machine_datetime(value: str) -> datetime | None:
    """只接受明确机器可读 ISO-8601 时刻；naive 视为无法可靠解析 → None。"""
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = datetime.fromisoformat(stripped)
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _extract_blocks(content_root: lxml_html.HtmlElement) -> list[ParsedBlock]:
    candidates: list[tuple[ParsedBlockType, str, lxml_html.HtmlElement]] = []
    selected: set = set()
    for element in content_root.iter():
        tag = element.tag
        if not isinstance(tag, str) or tag not in _BLOCK_TAGS:
            continue
        # 嵌套去重：祖先链已有被选中的容器块（li/blockquote/table）时跳过，
        # 其文本已并入外层容器的 text_content()，避免嵌套重复。
        if _has_selected_container(element, selected):
            continue
        block_type = _block_type_for(tag)
        text = _extract_block_text(element, block_type)
        if not text:
            continue
        candidates.append((block_type, text, element))
        if tag in _CONTAINER_TAGS:
            selected.add(element)
    # 相邻 (block_type, text) 完全相同 → 去重（保留首个）。
    deduped: list[tuple[ParsedBlockType, str, lxml_html.HtmlElement]] = []
    for item in candidates:
        if deduped and deduped[-1][0] == item[0] and deduped[-1][1] == item[1]:
            continue
        deduped.append(item)
    return [
        _make_block(ordinal, block_type, text, element)
        for ordinal, (block_type, text, element) in enumerate(deduped, start=1)
    ]


def _block_type_for(tag: str) -> ParsedBlockType:
    if tag in _HEADING_TAGS:
        return ParsedBlockType.HEADING
    if tag == "p":
        return ParsedBlockType.PARAGRAPH
    if tag == "li":
        return ParsedBlockType.LIST_ITEM
    if tag == "blockquote":
        return ParsedBlockType.BLOCKQUOTE
    return ParsedBlockType.TABLE_TEXT


def _extract_block_text(
    element: lxml_html.HtmlElement,
    block_type: ParsedBlockType,
) -> str:
    if block_type == ParsedBlockType.TABLE_TEXT:
        cells = element.xpath(".//td | .//th")
        parts = [_normalize(cell.text_content()) for cell in cells]
        return " ".join(part for part in parts if part)
    return _normalize(element.text_content())


def _has_selected_container(element: lxml_html.HtmlElement, selected: set) -> bool:
    parent = element.getparent()
    while parent is not None:
        if parent in selected:
            return True
        parent = parent.getparent()
    return False


def _make_block(
    ordinal: int,
    block_type: ParsedBlockType,
    text: str,
    element: lxml_html.HtmlElement,
) -> ParsedBlock:
    return ParsedBlock(
        ordinal=ordinal,
        block_type=block_type,
        text=text,
        text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        locator={
            "type": "html_dom",
            "ordinal": ordinal,
            "tag": element.tag,
            "xpath": _xpath_for(element),
            "element_id": element.get("id"),
        },
    )


def _xpath_for(element: lxml_html.HtmlElement) -> str:
    """绝对 xpath（1-based 同 tag 兄弟位置），同一 DOM 下稳定。"""
    parts: list[str] = []
    node = element
    while node is not None:
        tag = node.tag
        parent = node.getparent()
        if not isinstance(tag, str):
            # 注释 / PI（防御性跳过，不应出现于候选块）。
            node = parent
            continue
        if parent is None:
            parts.append(f"/{tag}")
            break
        same = [sibling for sibling in parent if sibling.tag == tag]
        if len(same) > 1:
            parts.append(f"/{tag}[{same.index(node) + 1}]")
        else:
            parts.append(f"/{tag}")
        node = parent
    return "".join(reversed(parts))


def _normalize(text: str) -> str:
    return " ".join(text.split())
