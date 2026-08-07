"""Unit tests for snapshot fingerprint v1 (stage 2C.2B).

覆盖：
- golden vector：固定输入 → 固定 SHA-256（canonical JSON 序列化/排序/
  Decimal str()/版本字段任一规则变化都会使测试失败）；
- 结果确定性：与输入顺序无关；
- 排除规则：fetched_at / request_count 不参与指纹（重复获取可 replay）；
- 敏感性：任何领域字段变化都会改变指纹。
"""

from datetime import UTC, date, datetime
from decimal import Decimal

from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)
from app.macro.contracts import (
    MacroFetchResult,
    MacroFrequency,
    MacroGeography,
    MacroGeographyType,
    MacroIndicator,
    MacroObservation,
    MacroPageInfo,
    MacroPeriodSemantics,
    MacroQuery,
    MacroTopic,
)
from app.macro.fingerprint import (
    MACRO_SNAPSHOT_FINGERPRINT_VERSION,
    WORLD_BANK_NORMALIZATION_VERSION,
    FingerprintArtifact,
    build_macro_snapshot_fingerprint,
)

GOLDEN = "15c9607b0800ffb2131f34f38b7af54ab7778cbc7c28ba0cda6cf142407767d5"

_QUERY = MacroQuery(
    provider_key="world_bank",
    indicator_code="SP.POP.TOTL",
    country_code="CHN",
    start_year=2020,
    end_year=2024,
)

_INDICATOR = MacroIndicator(
    provider_key="world_bank",
    external_indicator_id="SP.POP.TOTL",
    name="Population, total",
    unit="",
    source_id="2",
    source_name="World Development Indicators",
    source_note="Total population is based on the de facto definition.",
    source_organization="World Bank",
    topics=(
        MacroTopic(topic_id="19", name="Population: Structure, growth & density"),
        MacroTopic(topic_id="8", name="A second topic"),
    ),
)

_GEOGRAPHY = MacroGeography(
    geography_type=MacroGeographyType.COUNTRY,
    requested_code="CHN",
    provider_country_id="CHN",
    iso2_code="CN",
    iso3_code="CHN",
    name="China",
    region_name="East Asia & Pacific",
    income_level_name="Upper middle income",
)

_ARTIFACTS = (
    FingerprintArtifact(
        role=MacroSnapshotArtifactRole.INDICATOR_METADATA,
        page=None,
        sha256="a" * 64,
        response_status=200,
        final_hostname="api.worldbank.org",
        content_type="application/json",
    ),
    FingerprintArtifact(
        role=MacroSnapshotArtifactRole.COUNTRY_METADATA,
        page=None,
        sha256="b" * 64,
        response_status=200,
        final_hostname="api.worldbank.org",
        content_type="application/json",
    ),
    FingerprintArtifact(
        role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE,
        page=1,
        sha256="c" * 64,
        response_status=200,
        final_hostname="api.worldbank.org",
        content_type="application/json",
    ),
)


def _observations() -> tuple[MacroObservation, ...]:
    return tuple(
        MacroObservation(
            provider_key="world_bank",
            external_indicator_id="SP.POP.TOTL",
            geography_code="CHN",
            period=str(year),
            normalized_period_start=date(year, 1, 1),
            frequency=MacroFrequency.ANNUAL,
            value=Decimal(str(1400000000 + year)),
            is_missing=False,
            period_semantics=MacroPeriodSemantics.PROVIDER_YEAR_LABEL,
            observation_status="",
        )
        for year in range(2020, 2025)
    )


def _result(
    *,
    fetched_at: datetime = datetime(2026, 1, 1, tzinfo=UTC),
    request_count: int = 3,
) -> MacroFetchResult:
    return MacroFetchResult(
        provider_key="world_bank",
        query=_QUERY,
        indicator=_INDICATOR,
        geography=_GEOGRAPHY,
        observations=_observations(),
        page_info=MacroPageInfo(page=1, pages=1, per_page=1000, total=5, last_updated="2026-01-01"),
        fetched_at=fetched_at,
        request_count=request_count,
        acquisition_method=AcquisitionMethod.OFFICIAL_API,
        authority_tier=SourceAuthorityTier.TIER_1,
        critical_claim_eligible=True,
        provider_capabilities=(SourceCapability.DOCUMENT_DOWNLOAD, SourceCapability.MACRO_DATA),
    )


def test_golden_vector() -> None:
    assert build_macro_snapshot_fingerprint(_result(), _ARTIFACTS) == GOLDEN


def test_result_order_independent() -> None:
    result = _result()
    reversed_observations = tuple(reversed(result.observations))
    import dataclasses

    other = dataclasses.replace(result, observations=reversed_observations)
    assert build_macro_snapshot_fingerprint(other, _ARTIFACTS) == GOLDEN


def test_artifact_order_independent() -> None:
    reordered = tuple(reversed(_ARTIFACTS))
    assert build_macro_snapshot_fingerprint(_result(), reordered) == GOLDEN


def test_fetched_at_excluded() -> None:
    later = datetime(2030, 6, 15, 12, 30, tzinfo=UTC)
    assert build_macro_snapshot_fingerprint(_result(fetched_at=later), _ARTIFACTS) == GOLDEN


def test_request_count_excluded() -> None:
    assert build_macro_snapshot_fingerprint(_result(request_count=7), _ARTIFACTS) == GOLDEN


def test_fingerprint_versions_frozen() -> None:
    assert MACRO_SNAPSHOT_FINGERPRINT_VERSION == 1
    assert WORLD_BANK_NORMALIZATION_VERSION == "world_bank_v1"


def test_sensitive_to_value_change() -> None:
    import dataclasses

    observations = list(_observations())
    observations[0] = dataclasses.replace(observations[0], value=Decimal("999999999"))
    other = dataclasses.replace(_result(), observations=tuple(observations))
    assert build_macro_snapshot_fingerprint(other, _ARTIFACTS) != GOLDEN


def test_sensitive_to_indicator_change() -> None:
    import dataclasses

    other = dataclasses.replace(_result(), indicator=dataclasses.replace(_INDICATOR, name="x"))
    assert build_macro_snapshot_fingerprint(other, _ARTIFACTS) != GOLDEN


def test_sensitive_to_artifact_sha_change() -> None:
    changed = list(_ARTIFACTS)
    artifact = changed[-1]
    changed[-1] = FingerprintArtifact(
        role=artifact.role,
        page=artifact.page,
        sha256="d" * 64,
        response_status=artifact.response_status,
        final_hostname=artifact.final_hostname,
        content_type=artifact.content_type,
    )
    assert build_macro_snapshot_fingerprint(_result(), tuple(changed)) != GOLDEN
