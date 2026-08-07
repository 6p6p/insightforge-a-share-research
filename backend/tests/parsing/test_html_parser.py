"""HTML parser unit tests (stage 2E.1, §8 parser 部分)。

全部为确定性纯函数测试（不联网、不访问 DB），覆盖：
- article / main / body 内容根 fallback；
- script/style/noscript/template/svg 噪声排除（含 svg 内 <title> 不污染标题）；
- DOM 顺序抽取 h1-h6 / p / li / blockquote / table；
- 嵌套去重（li 内 p、blockquote 内 p 不重复）；
- whitespace normalize；空文本跳过；相邻完全相同 block 去重；
- title 优先级 og:title → <title> → h1 → None；
- published_at 只接受明确机器可读元数据（naive / 无效 → None）；
- 中文 UTF-8 / GBK 编码；
- locator（element_id / 绝对 xpath）与确定性输出；
- 空输入 / 纯空白 → HtmlParseError。
"""

import hashlib

import pytest

from app.domain.parsing import ParsedBlockType
from app.parsing.errors import HtmlParseError
from app.parsing.html_parser import parse_html_bytes


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _doc(html: str):
    return parse_html_bytes(html.encode("utf-8"))


# ---------------------------------------------------------- 内容根 fallback


def test_content_root_prefers_article_over_main_and_body() -> None:
    doc = _doc(
        "<html><body><main><p>main 正文</p></main>"
        "<article><h1>a</h1><p>article 正文</p></article></body></html>"
    )
    texts = [b.text for b in doc.blocks]
    assert texts == ["a", "article 正文"]  # article 优先，main 内 p 被忽略


def test_content_root_falls_back_to_main_when_no_article() -> None:
    doc = _doc("<html><body><main><h2>m</h2><p>main 正文</p></main></body></html>")
    texts = [b.text for b in doc.blocks]
    assert texts == ["m", "main 正文"]


def test_content_root_falls_back_to_body_when_no_article_or_main() -> None:
    doc = _doc("<html><body><h3>h</h3><p>body 正文</p></body></html>")
    texts = [b.text for b in doc.blocks]
    assert texts == ["h", "body 正文"]


def test_content_root_falls_back_to_document_when_no_body() -> None:
    doc = _doc("<html><p>裸正文</p></html>")
    assert [b.text for b in doc.blocks] == ["裸正文"]


# ---------------------------------------------------------- 噪声排除


def test_noise_tags_are_removed() -> None:
    doc = _doc(
        "<html><body>"
        "<script>var x=1</script>"
        "<style>.a{}</style>"
        "<noscript>nojs</noscript>"
        "<template><p>template 内容</p></template>"
        "<svg><title>svg title</title><text>svg text</text></svg>"
        "<p>正文</p>"
        "</body></html>"
    )
    texts = [b.text for b in doc.blocks]
    assert texts == ["正文"]
    assert doc.extracted_title is None  # svg 内 <title> 已被删除，不得成为标题


# ---------------------------------------------------------- 抽取规则


def test_extracts_heading_paragraph_list_blockquote_table() -> None:
    doc = _doc(
        "<html><body><article>"
        "<h1>标题一</h1><h2>标题二</h2>"
        "<p>段一</p>"
        "<ul><li>列表一</li><li>列表二</li></ul>"
        "<blockquote>引用</blockquote>"
        "<table><tr><td>指标</td><td>数值</td></tr></table>"
        "</article></body></html>"
    )
    types = [b.block_type for b in doc.blocks]
    assert types == [
        ParsedBlockType.HEADING,
        ParsedBlockType.HEADING,
        ParsedBlockType.PARAGRAPH,
        ParsedBlockType.LIST_ITEM,
        ParsedBlockType.LIST_ITEM,
        ParsedBlockType.BLOCKQUOTE,
        ParsedBlockType.TABLE_TEXT,
    ]
    texts = [b.text for b in doc.blocks]
    assert texts[-1] == "指标 数值"


def test_nested_containers_do_not_duplicate_text() -> None:
    doc = _doc(
        "<html><body><article>"
        "<blockquote><p>引用段落</p></blockquote>"
        "<ul><li><p>列表内段落</p></li></ul>"
        "</article></body></html>"
    )
    types = [b.block_type for b in doc.blocks]
    texts = [b.text for b in doc.blocks]
    assert types == [ParsedBlockType.BLOCKQUOTE, ParsedBlockType.LIST_ITEM]
    assert texts == ["引用段落", "列表内段落"]


def test_heading_order_preserves_dom_order() -> None:
    doc = _doc(
        "<html><body><article><h3>c</h3><p>p1</p><h1>a</h1><p>p2</p></article></body></html>"
    )
    assert [b.text for b in doc.blocks] == ["c", "p1", "a", "p2"]


# ---------------------------------------------------------- normalize / 空 / 去重


def test_whitespace_normalized() -> None:
    doc = _doc("<html><body><p>  第一段\n  正文\t继续。  </p></body></html>")
    assert doc.blocks[0].text == "第一段 正文 继续。"
    assert doc.blocks[0].text_sha256 == _sha("第一段 正文 继续。")


def test_empty_text_blocks_skipped() -> None:
    doc = _doc("<html><body><p>   </p><p>非空</p></body></html>")
    assert [b.text for b in doc.blocks] == ["非空"]


def test_adjacent_identical_blocks_deduplicated() -> None:
    doc = _doc(
        "<html><body><p>相同段落</p><p>相同段落</p><p>不同段落</p><p>相同段落</p></body></html>"
    )
    assert [b.text for b in doc.blocks] == ["相同段落", "不同段落", "相同段落"]


# ---------------------------------------------------------- title 优先级


def test_title_prefers_og_title_over_document_title() -> None:
    doc = _doc(
        '<html><head><meta property="og:title" content="og 标题">'
        "<title>文档标题</title></head><body><h1>h1 标题</h1></body></html>"
    )
    assert doc.extracted_title == "og 标题"


def test_title_falls_back_to_document_title() -> None:
    doc = _doc("<html><head><title>  文档标题  </title></head><body><h1>h1 标题</h1></body></html>")
    assert doc.extracted_title == "文档标题"


def test_title_falls_back_to_h1() -> None:
    doc = _doc("<html><body><article><h1>  h1 标题  </h1></article></body></html>")
    assert doc.extracted_title == "h1 标题"


def test_title_none_when_no_source() -> None:
    doc = _doc("<html><body><p>无标题</p></body></html>")
    assert doc.extracted_title is None


# ---------------------------------------------------------- published_at


def test_published_at_from_meta_with_offset() -> None:
    doc = _doc(
        '<html><head><meta property="article:published_time" '
        'content="2026-08-07T09:30:00+08:00"></head><body><p>正文</p></body></html>'
    )
    assert doc.extracted_published_at is not None
    assert doc.extracted_published_at.utcoffset() is not None
    assert doc.extracted_published_at.isoformat() == "2026-08-07T09:30:00+08:00"


def test_published_at_from_time_datetime() -> None:
    doc = _doc('<html><body><time datetime="2026-08-07T01:00:00Z">时间</time></body></html>')
    assert doc.extracted_published_at is not None
    assert doc.extracted_published_at.isoformat() == "2026-08-07T01:00:00+00:00"


def test_published_at_rejects_naive_datetime() -> None:
    # naive 本地时间无法可靠确定绝对时刻 → None（不得伪造时区）。
    doc = _doc(
        '<html><head><meta property="article:published_time" '
        'content="2026-08-07T09:30:00"></head><body><p>正文</p></body></html>'
    )
    assert doc.extracted_published_at is None


def test_published_at_rejects_invalid_value() -> None:
    doc = _doc(
        '<html><head><meta property="article:published_time" '
        'content="not-a-date"></head><body><p>正文</p></body></html>'
    )
    assert doc.extracted_published_at is None


def test_published_at_none_when_absent() -> None:
    doc = _doc("<html><body><p>正文</p></body></html>")
    assert doc.extracted_published_at is None


# ---------------------------------------------------------- 中文编码


def test_chinese_utf8_roundtrip() -> None:
    html = "<html><body><p>中文段落：确定性解析。</p></body></html>"
    doc = _doc(html)
    assert doc.blocks[0].text == "中文段落：确定性解析。"


def test_chinese_gbk_encoding_detected() -> None:
    html = '<html><head><meta charset="gbk"></head><body><p>中文GBK编码段落。</p></body></html>'
    doc = parse_html_bytes(html.encode("gbk"))
    assert doc.blocks[0].text == "中文GBK编码段落。"


# ---------------------------------------------------------- locator


def test_locator_carries_element_id_and_absolute_xpath() -> None:
    doc = _doc('<html><body><article><p id="first">第一段</p><p>第二段</p></article></body></html>')
    first, second = doc.blocks
    assert first.locator == {
        "type": "html_dom",
        "ordinal": 1,
        "tag": "p",
        "xpath": "/html/body/article/p[1]",
        "element_id": "first",
    }
    assert second.locator == {
        "type": "html_dom",
        "ordinal": 2,
        "tag": "p",
        "xpath": "/html/body/article/p[2]",
        "element_id": None,
    }


def test_locator_stable_across_reparse() -> None:
    html = "<html><body><article><h1>x</h1><p>正文</p></article></body></html>"
    first = _doc(html)
    second = _doc(html)
    assert [b.locator for b in first.blocks] == [b.locator for b in second.blocks]
    assert [b.text_sha256 for b in first.blocks] == [b.text_sha256 for b in second.blocks]


# ---------------------------------------------------------- 确定性


def test_parse_is_deterministic() -> None:
    html = "<html><body><p>确定性输出</p></body></html>"
    doc_a = _doc(html)
    doc_b = _doc(html)
    assert doc_a == doc_b
    assert doc_a.raw_content_sha256 == doc_b.raw_content_sha256


def test_different_input_yields_different_fingerprint_material() -> None:
    doc_a = _doc("<html><body><p>甲</p></body></html>")
    doc_b = _doc("<html><body><p>乙</p></body></html>")
    assert doc_a.raw_content_sha256 != doc_b.raw_content_sha256


# ---------------------------------------------------------- 空输入


@pytest.mark.parametrize("raw", [b"", b"   \n\t  "])
def test_empty_or_blank_input_raises(raw: bytes) -> None:
    with pytest.raises(HtmlParseError):
        parse_html_bytes(raw)


def test_empty_page_yields_zero_blocks() -> None:
    doc = _doc("<html><head></head><body></body></html>")
    assert doc.blocks == ()
    assert doc.extracted_title is None
