"""Tests for default source registry definitions."""

from app.source_registry.defaults import DEFAULT_PROVIDERS


def test_default_provider_count() -> None:
    assert len(DEFAULT_PROVIDERS) == 11


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
    } == keys


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
