"""Tests for source registry domain enums."""

from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
    SourceProviderType,
)


def test_authority_tiers_are_integers_1_to_4() -> None:
    assert [int(tier) for tier in SourceAuthorityTier] == [1, 2, 3, 4]
    assert isinstance(SourceAuthorityTier.TIER_1, int)


def test_provider_types() -> None:
    values = [provider.value for provider in SourceProviderType]
    assert "exchange" in values
    assert "regulator" in values
    assert "general_web" in values
    assert not any("model" in value for value in values)


def test_capabilities() -> None:
    values = [cap.value for cap in SourceCapability]
    assert "company_announcement" in values
    assert "macro_data" in values
    assert "news" in values


def test_acquisition_methods_include_model_discovery() -> None:
    values = [method.value for method in AcquisitionMethod]
    assert "model_web_search_discovery" in values
    assert "official_api" in values
