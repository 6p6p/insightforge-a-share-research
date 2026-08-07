"""Tests for OriginalPublisherResolver (stage 2D.2A, §二十二).

把 Candidate 的 normalized URL 解析为 Source Registry 登记的 Original
Publisher。纯函数、零网络。覆盖：
- root / 子域 / apex 域名归属（is_url_allowed 的"等于或真实子域"语义）；
- evil 复合域名 / 子域劫持拒绝（非 substring 匹配）；
- 协议 / userinfo / 非默认端口 / IP 字面量 host 拒绝；
- 显式 :443 等价无端口；
- 资格过滤：enabled / news_article / public_html 缺一不可；
- 多 Publisher 重叠命中 → NewsPublisherAmbiguous，不自动挑选。
"""

import pytest

from app.db.models.source_provider import SourceProviderModel
from app.news.errors import NewsPublisherAmbiguous, NewsPublisherUnsupported
from app.news.publisher_resolver import OriginalPublisherResolver


def _provider(
    key: str,
    domains: list[str],
    *,
    capabilities: tuple[str, ...] = ("news_article",),
    methods: tuple[str, ...] = ("public_html",),
    enabled: bool = True,
) -> SourceProviderModel:
    return SourceProviderModel(
        provider_key=key,
        display_name=key,
        provider_type="media",
        authority_tier=3,
        homepage_url=f"https://www.{domains[0]}",
        allowed_domains=list(domains),
        capabilities=list(capabilities),
        acquisition_methods=list(methods),
        exchange_scope=[],
        requires_api_key=False,
        critical_claim_eligible=False,
        enabled=enabled,
    )


_XINHUA = _provider("xinhuanet", ["xinhuanet.com"])
_CNSTOCK = _provider("cnstock", ["cnstock.com"])
_CS_COM_CN = _provider("cs_com_cn", ["cs.com.cn"])
_PUBLISHERS = [_XINHUA, _CNSTOCK, _CS_COM_CN]


def _resolve(url: str, providers=None):
    chosen = providers if providers is not None else _PUBLISHERS
    return OriginalPublisherResolver.resolve(url, chosen)


def test_resolves_www_root() -> None:
    assert _resolve("https://www.xinhuanet.com/2026/0807/0001.htm").provider_key == "xinhuanet"


def test_resolves_subdomain() -> None:
    assert _resolve("https://finance.xinhuanet.com/2026/0807/0002.htm").provider_key == "xinhuanet"


def test_resolves_apex_domain() -> None:
    assert _resolve("https://cnstock.com/2026/0807/a.htm").provider_key == "cnstock"


def test_resolves_paper_subdomain() -> None:
    assert _resolve("https://paper.cnstock.com/2026/0807/b.htm").provider_key == "cnstock"


def test_resolves_cs_com_cn() -> None:
    assert _resolve("https://www.cs.com.cn/2026/0807/c.htm").provider_key == "cs_com_cn"


def test_rejects_evil_compound_domain() -> None:
    # evilcnstock.com 不是 cnstock.com 的子域，也不能 substring 匹配
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://evilcnstock.com/a.htm")


def test_rejects_subdomain_hijack() -> None:
    # cnstock.com.evil.example 是 evil.example 的子域，不是 cnstock.com 的子域
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://cnstock.com.evil.example/a.htm")


def test_rejects_unknown_domain() -> None:
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://unknown.example.com/a.htm")


def test_rejects_http_scheme() -> None:
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("http://www.xinhuanet.com/a.htm")


def test_rejects_userinfo() -> None:
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://user@www.xinhuanet.com/a.htm")


def test_rejects_non_default_port() -> None:
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://www.xinhuanet.com:8443/a.htm")


def test_accepts_explicit_443() -> None:
    # 显式 :443 视为等价无端口，仍可解析
    assert _resolve("https://www.xinhuanet.com:443/a.htm").provider_key == "xinhuanet"


def test_rejects_ip_literal_host() -> None:
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://93.184.216.34/a.htm")


def test_rejects_disabled_provider() -> None:
    providers = [_provider("xinhuanet", ["xinhuanet.com"], enabled=False)]
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://www.xinhuanet.com/a.htm", providers)


def test_rejects_missing_news_article_capability() -> None:
    providers = [_provider("xinhuanet", ["xinhuanet.com"], capabilities=("macro_data",))]
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://www.xinhuanet.com/a.htm", providers)


def test_rejects_missing_public_html_method() -> None:
    providers = [_provider("xinhuanet", ["xinhuanet.com"], methods=("official_web_page",))]
    with pytest.raises(NewsPublisherUnsupported):
        _resolve("https://www.xinhuanet.com/a.htm", providers)


def test_raises_ambiguous_on_overlapping_domains() -> None:
    providers = [
        _provider("xinhuanet", ["xinhuanet.com"]),
        _provider("xinhuanet_alt", ["xinhuanet.com"]),
    ]
    with pytest.raises(NewsPublisherAmbiguous):
        _resolve("https://www.xinhuanet.com/a.htm", providers)
