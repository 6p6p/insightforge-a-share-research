"""Tests for URL safety policy."""

from app.source_registry.url_policy import is_url_allowed


def test_allows_approved_urls() -> None:
    allowed = [
        "sse.com.cn",
        "cninfo.com.cn",
        "szse.cn",
        "stats.gov.cn",
        "worldbank.org",
    ]
    assert is_url_allowed("https://www.sse.com.cn", allowed)
    assert is_url_allowed("https://static.cninfo.com.cn/example.pdf", allowed)
    assert is_url_allowed("https://disc.static.szse.cn/example.pdf", allowed)
    assert is_url_allowed("https://data.stats.gov.cn/", allowed)
    assert is_url_allowed("https://api.worldbank.org/v2/example", allowed)


def test_rejects_bad_urls() -> None:
    allowed = ["sse.com.cn", "cninfo.com.cn"]
    assert not is_url_allowed("http://www.sse.com.cn", allowed)
    assert not is_url_allowed("https://evil-cninfo.com.cn", allowed)
    assert not is_url_allowed("https://cninfo.com.cn.evil.com", allowed)
    assert not is_url_allowed("https://user:pass@www.sse.com.cn", allowed)
    assert not is_url_allowed("sse.com.cn@example.com", allowed)
    assert not is_url_allowed("not a url", allowed)
    assert not is_url_allowed("https://www.sse.com.cn:8080/x", allowed)


def test_empty_allowed_domains() -> None:
    assert not is_url_allowed("https://www.sse.com.cn", [])
