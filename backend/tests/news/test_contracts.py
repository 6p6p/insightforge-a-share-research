"""Unit tests for news discovery contracts (stage 2D.1).

覆盖 §六/§七：
- NewsDiscoveryQuery 8 条校验规则；
- NewsDiscoveryCandidate 规则与 URL normalization（fragment 删除、默认端口、
  IDNA、userinfo 拒绝、discovered_url 保留原始语义）；
- normalize_discovery_url 确定性。
"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import (
    NewsDiscoveryCandidate,
    NewsDiscoveryQuery,
    normalize_discovery_url,
)
from app.news.errors import NewsDiscoveryInvalidQuery

_COMPANY_ID = UUID("11111111-2222-3333-4444-555555555555")
_BASE = dict(
    company_id=_COMPANY_ID,
    query_text="Kweichow Moutai",
    start_at=datetime(2026, 8, 1, tzinfo=UTC),
    end_at=datetime(2026, 8, 7, tzinfo=UTC),
    max_results=10,
)


def _query(**overrides: object) -> NewsDiscoveryQuery:
    values = dict(_BASE)
    values.update(overrides)
    return NewsDiscoveryQuery(**values)


# ---------------------------------------------------------------- query 校验


def test_query_valid() -> None:
    q = _query()
    assert q.company_id == _COMPANY_ID
    assert q.query_text == "Kweichow Moutai"
    assert q.max_results == 10


def test_query_default_max_results() -> None:
    q = _query()
    values = {k: v for k, v in _BASE.items() if k != "max_results"}
    q = NewsDiscoveryQuery(**values)
    assert q.max_results == 50


def test_query_trims_query_text() -> None:
    q = _query(query_text="  Kweichow Moutai  ")
    assert q.query_text == "Kweichow Moutai"


def test_query_rejects_empty_query() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery) as exc:
        _query(query_text="   ")
    assert exc.value.code == "news_discovery_invalid_query"


def test_query_rejects_crlf_nul() -> None:
    for bad in ("a\nb", "a\rb", "a\x00b"):
        with pytest.raises(NewsDiscoveryInvalidQuery):
            _query(query_text=bad)


def test_query_rejects_too_long_query() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _query(query_text="x" * 301)


def test_query_rejects_naive_datetime() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _query(start_at=datetime(2026, 8, 1))
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _query(end_at=datetime(2026, 8, 7))


def test_query_rejects_reversed_window() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _query(start_at=datetime(2026, 8, 7, tzinfo=UTC), end_at=datetime(2026, 8, 1, tzinfo=UTC))


def test_query_rejects_window_over_365_days() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _query(
            start_at=datetime(2025, 1, 1, tzinfo=UTC),
            end_at=datetime(2026, 1, 2, tzinfo=UTC),  # 366 天
        )


def test_query_accepts_365_day_window() -> None:
    q = _query(
        start_at=datetime(2025, 1, 1, tzinfo=UTC),
        end_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert q.start_at <= q.end_at


def test_query_rejects_future_window() -> None:
    future = datetime.now(UTC) + timedelta(minutes=10)
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _query(start_at=datetime(2026, 1, 1, tzinfo=UTC), end_at=future)


def test_query_rejects_max_results_out_of_range() -> None:
    for bad in (0, 101):
        with pytest.raises(NewsDiscoveryInvalidQuery):
            _query(max_results=bad)


def test_query_rejects_max_results_non_int() -> None:
    for bad in (True, "10", 10.0):
        with pytest.raises(NewsDiscoveryInvalidQuery):
            _query(max_results=bad)


def test_query_accepts_max_results_boundaries() -> None:
    assert _query(max_results=1).max_results == 1
    assert _query(max_results=100).max_results == 100


# ---------------------------------------------------------------- URL normalization


def test_normalize_removes_fragment_and_default_port() -> None:
    normalized = normalize_discovery_url("https://www.example.com:443/a/b?utm=1&q=2#section")
    assert normalized == "https://www.example.com/a/b?utm=1&q=2"


def test_normalize_http_default_port() -> None:
    assert normalize_discovery_url("http://example.com:80/x") == "http://example.com/x"


def test_normalize_lowercases_hostname_and_scheme() -> None:
    assert normalize_discovery_url("HTTPS://EXAMPLE.COM/A") == "https://example.com/A"


def test_normalize_keeps_non_default_port() -> None:
    assert normalize_discovery_url("https://example.com:8443/x") == "https://example.com:8443/x"


def test_normalize_idna_hostname() -> None:
    normalized = normalize_discovery_url("https://例子.测试/a")
    assert normalized == "https://xn--fsqu00a.xn--0zwm56d/a"


def test_normalize_keeps_tracking_params_and_query_order() -> None:
    assert normalize_discovery_url("https://example.com/p?a=1&utm_source=x&b=2") == (
        "https://example.com/p?a=1&utm_source=x&b=2"
    )


def test_normalize_rejects_userinfo() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        normalize_discovery_url("https://user:pass@example.com/x")


def test_normalize_rejects_bad_scheme() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        normalize_discovery_url("ftp://example.com/x")


def test_normalize_rejects_empty_hostname() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        normalize_discovery_url("https:///x")


# ---------------------------------------------------------------- candidate 校验


def _candidate(**overrides: object) -> NewsDiscoveryCandidate:
    values: dict = {
        "rank": 1,
        "title": "A headline",
        "discovered_url": "https://news.example.com/a?utm=1#frag",
        "seen_at": datetime(2026, 8, 7, 5, 0, 0, tzinfo=UTC),
        "engine": NewsDiscoveryEngine.GDELT_DOC,
        "source_language": "English",
        "source_country": "United States",
    }
    values.update(overrides)
    return NewsDiscoveryCandidate(**values)


def test_candidate_normalizes_url() -> None:
    c = _candidate()
    assert c.normalized_url == "https://news.example.com/a?utm=1"
    assert c.domain == "news.example.com"


def test_candidate_accepts_http_url() -> None:
    # Candidate 只是线索，尚未网络访问：允许 http。
    c = _candidate(discovered_url="http://news.example.com/x")
    assert c.normalized_url == "http://news.example.com/x"


def test_candidate_preserves_discovered_url_original_semantics() -> None:
    c = _candidate(discovered_url="  https://example.com:443/a/b  ")
    assert c.discovered_url == "https://example.com:443/a/b"
    assert c.normalized_url == "https://example.com/a/b"


def test_candidate_rejects_rank_non_positive() -> None:
    for bad in (0, -1):
        with pytest.raises(NewsDiscoveryInvalidQuery):
            _candidate(rank=bad)
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(rank=True)


def test_candidate_rejects_blank_title() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(title="   ")


def test_candidate_rejects_title_too_long() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(title="x" * 1001)


def test_candidate_rejects_naive_seen_at() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(seen_at=datetime(2026, 8, 7, 5, 0, 0))


def test_candidate_rejects_userinfo_url() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(discovered_url="https://user:pass@example.com/x")


def test_candidate_rejects_inconsistent_normalized_url() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(normalized_url="https://wrong.example.com/y")


def test_candidate_rejects_inconsistent_domain() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(domain="wrong.example.com")


def test_candidate_blank_language_country_to_none() -> None:
    # 契约层规则：空串/纯空白 → None；非空原样保留（trim 由 Parser 负责，
    # 契约层不改变语义）。
    c = _candidate(source_language="  ", source_country="  China  ")
    assert c.source_language is None
    assert c.source_country == "  China  "


def test_candidate_rejects_wrong_engine() -> None:
    with pytest.raises(NewsDiscoveryInvalidQuery):
        _candidate(engine="not_an_engine")
