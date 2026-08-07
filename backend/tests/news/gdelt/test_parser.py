"""Unit tests for GDELT DOC 2.0 artlist parser (stage 2D.1).

覆盖 §十一：
- 宽容单条（缺 url/title、非法 URL、日期无法解析 → 跳过，不让整个查询失败）；
- 严格结构（顶层非 object / articles 类型错误 → 拒绝整个 payload）；
- rank 过滤后从 1 重排；同一 normalized_url 去重保留第一条；
- domain 由 normalized URL hostname 派生，不盲信 Provider 字段；
- source language/country trim、空→None；输出顺序稳定。
"""

from datetime import UTC, datetime

import pytest

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.gdelt.errors import GdeltMalformedResponse
from app.news.gdelt.parser import GdeltDocParser


def _article(**overrides: object) -> dict:
    values: dict = {
        "url": "https://news.example.com/a?utm=1",
        "title": "A headline",
        "seendate": "20260807050000",
        "domain": "provider.example.com",  # 故意与 URL hostname 不一致，验证派生
        "language": "English",
        "sourcecountry": "United States",
    }
    values.update(overrides)
    return values


def test_normal_article() -> None:
    (candidate,) = GdeltDocParser.parse({"articles": [_article()]})
    assert candidate.rank == 1
    assert candidate.title == "A headline"
    assert candidate.discovered_url == "https://news.example.com/a?utm=1"
    assert candidate.normalized_url == "https://news.example.com/a?utm=1"
    assert candidate.seen_at == datetime(2026, 8, 7, 5, 0, 0, tzinfo=UTC)
    assert candidate.engine == NewsDiscoveryEngine.GDELT_DOC
    assert candidate.source_language == "English"
    assert candidate.source_country == "United States"


def test_domain_derived_from_url_not_provider_field() -> None:
    (candidate,) = GdeltDocParser.parse({"articles": [_article()]})
    assert candidate.domain == "news.example.com"


def test_empty_articles() -> None:
    assert GdeltDocParser.parse({"articles": []}) == ()


def test_missing_articles_key_is_empty() -> None:
    assert GdeltDocParser.parse({}) == ()


def test_bad_top_level_rejected() -> None:
    for payload in ("not an object", [1, 2], 42, None):
        with pytest.raises(GdeltMalformedResponse):
            GdeltDocParser.parse(payload)


def test_bad_articles_type_rejected() -> None:
    with pytest.raises(GdeltMalformedResponse):
        GdeltDocParser.parse({"articles": "not-a-list"})


def test_missing_url_skipped() -> None:
    payload = {"articles": [_article(url=""), _article()]}
    candidates = GdeltDocParser.parse(payload)
    assert len(candidates) == 1
    assert candidates[0].title == "A headline"


def test_missing_title_skipped() -> None:
    payload = {"articles": [_article(title="   "), _article()]}
    candidates = GdeltDocParser.parse(payload)
    assert len(candidates) == 1


def test_invalid_url_skipped() -> None:
    payload = {"articles": [_article(url="ftp://bad.example.com/x"), _article()]}
    candidates = GdeltDocParser.parse(payload)
    assert len(candidates) == 1


def test_invalid_date_skipped() -> None:
    payload = {"articles": [_article(seendate="not-a-date"), _article()]}
    candidates = GdeltDocParser.parse(payload)
    assert len(candidates) == 1


def test_non_object_article_skipped() -> None:
    payload = {"articles": ["not-an-object", _article()]}
    candidates = GdeltDocParser.parse(payload)
    assert len(candidates) == 1


def test_language_country_optional_and_trimmed() -> None:
    payload = {"articles": [_article(language="  ", sourcecountry="  China  ")]}
    (candidate,) = GdeltDocParser.parse(payload)
    assert candidate.source_language is None
    assert candidate.source_country == "China"


def test_duplicate_normalized_url_deduped() -> None:
    payload = {
        "articles": [
            _article(url="https://news.example.com/a?utm=1"),
            _article(url="https://news.example.com:443/a?utm=1", title="dup of first"),
            _article(url="https://news.example.com/a?utm=2", title="different query"),
        ]
    }
    candidates = GdeltDocParser.parse(payload)
    assert len(candidates) == 2
    assert candidates[0].title == "A headline"
    assert candidates[1].title == "different query"


def test_rank_reindexed_after_filter() -> None:
    payload = {
        "articles": [
            _article(seendate="not-a-date"),  # 被过滤
            _article(),
            _article(url="https://news.example.com/b"),
        ]
    }
    candidates = GdeltDocParser.parse(payload)
    assert [c.rank for c in candidates] == [1, 2]


def test_stable_output_order() -> None:
    payload = {"articles": [_article(), _article(url="https://news.example.com/b")]}
    first = GdeltDocParser.parse(payload)
    second = GdeltDocParser.parse(payload)
    assert [c.normalized_url for c in first] == [c.normalized_url for c in second]
