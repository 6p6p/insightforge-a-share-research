"""Tests for default source registry definitions."""

from app.source_registry.defaults import DEFAULT_PROVIDERS


def test_default_provider_count() -> None:
    assert len(DEFAULT_PROVIDERS) == 14


def test_provider_keys_unique() -> None:
    keys = [provider.provider_key for provider in DEFAULT_PROVIDERS]
    assert len(keys) == len(set(keys))


def test_nbs_has_no_official_api() -> None:
    nbs = next(p for p in DEFAULT_PROVIDERS if p.provider_key == "nbs")
    assert "official_api" not in [m.value for m in nbs.acquisition_methods]


def test_fred_requires_api_key_and_official_api() -> None:
    fred = next(p for p in DEFAULT_PROVIDERS if p.provider_key == "fred")
    assert "official_api" in [m.value for m in fred.acquisition_methods]
    assert fred.requires_api_key is True


def test_no_model_or_search_provider() -> None:
    assert not any("search" in p.provider_key for p in DEFAULT_PROVIDERS)
    assert not any("model" in p.provider_key for p in DEFAULT_PROVIDERS)


def test_no_gdelt_or_llm_provider() -> None:
    keys = {p.provider_key for p in DEFAULT_PROVIDERS}
    assert {"gdelt", "gdelt_doc", "openai", "chatgpt", "search_engine"}.isdisjoint(keys)


def test_provider_keys_exact_set() -> None:
    keys = {p.provider_key for p in DEFAULT_PROVIDERS}
    assert {
        "sse",
        "szse",
        "bse",
        "cninfo",
        "csrc",
        "nbs",
        "fred",
        "world_bank",
        "xinhuanet",
        "cnstock",
        "cs_com_cn",
        "eastmoney",
        "issuer_official",
        "user_supplied",
    } == keys


def test_eastmoney_provider_definition() -> None:
    eastmoney = next(p for p in DEFAULT_PROVIDERS if p.provider_key == "eastmoney")
    assert set(eastmoney.allowed_domains) == {"eastmoney.com", "dfcfw.com"}
    assert eastmoney.authority_tier == 3
    assert eastmoney.critical_claim_eligible is False
    assert "company_announcement" in [c.value for c in eastmoney.capabilities]
    assert "issuer_ir" in [c.value for c in eastmoney.capabilities]
    assert "document_download" in [c.value for c in eastmoney.capabilities]
    assert "automatic_discovery" in [m.value for m in eastmoney.acquisition_methods]
    assert set(eastmoney.exchange_scope) == {"SSE", "SZSE", "BSE"}


def test_special_providers_have_empty_allowed_domains() -> None:
    for key in ("issuer_official", "user_supplied"):
        provider = next(p for p in DEFAULT_PROVIDERS if p.provider_key == key)
        assert provider.allowed_domains == []
        assert provider.homepage_url == "https://example.com"


def test_issuer_official_provider_definition() -> None:
    issuer = next(p for p in DEFAULT_PROVIDERS if p.provider_key == "issuer_official")
    assert issuer.provider_type == "issuer"
    assert issuer.authority_tier == 2
    assert issuer.critical_claim_eligible is True
    assert "issuer_ir" in [c.value for c in issuer.capabilities]


def test_user_supplied_provider_definition() -> None:
    user = next(p for p in DEFAULT_PROVIDERS if p.provider_key == "user_supplied")
    assert user.authority_tier == 4
    assert user.critical_claim_eligible is False
    assert "user_supplied" in [m.value for m in user.acquisition_methods]


def test_news_media_providers_are_tier3_public_html() -> None:
    media_keys = {"xinhuanet", "cnstock", "cs_com_cn"}
    for provider in DEFAULT_PROVIDERS:
        if provider.provider_key in media_keys:
            assert provider.provider_type == "media"
            assert provider.authority_tier == 3
            assert "news_article" in [c.value for c in provider.capabilities]
            assert "public_html" in [m.value for m in provider.acquisition_methods]
            assert provider.critical_claim_eligible is False
            assert provider.enabled is True
            assert provider.exchange_scope == []
