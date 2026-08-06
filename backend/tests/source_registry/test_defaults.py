"""Tests for default source registry definitions."""

from app.source_registry.defaults import DEFAULT_PROVIDERS


def test_default_provider_count() -> None:
    assert len(DEFAULT_PROVIDERS) == 8


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


def test_exchange_providers_exist() -> None:
    keys = {p.provider_key for p in DEFAULT_PROVIDERS}
    assert {"sse", "szse", "bse", "cninfo", "csrc", "nbs", "fred", "world_bank"} == keys
