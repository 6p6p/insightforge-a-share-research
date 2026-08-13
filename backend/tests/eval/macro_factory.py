"""Minimal valid `FrozenMacroSnapshotRef` factory for unit tests (stage 7B.1.4B.2)。

contract 要求 macro closure 必填（series / snapshot / observations / artifact_links /
raw_artifacts 均 >=1），本工厂构造一个自洽的最小 closure，供只关心
`snapshot_fingerprint` / `payload_sha256` / `fetched_at` 语义的单测复用，
避免在每个测试里重复铺 closure 样板。
"""

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4

from app.eval.contracts import (
    FrozenMacroArtifactLinkRef,
    FrozenMacroObservationRef,
    FrozenMacroRawArtifactRef,
    FrozenMacroSeriesRef,
    FrozenMacroSnapshotDetail,
    FrozenMacroSnapshotRef,
)


def make_macro_ref(
    *,
    snapshot_fingerprint: str,
    payload_sha256: str,
    fetched_at: datetime | None = None,
) -> FrozenMacroSnapshotRef:
    """构造一个自洽（1 series + 1 snapshot + 1 observation + 1 link + 1 raw）的 macro ref。"""
    fetched_at = fetched_at if fetched_at is not None else datetime(2026, 8, 1, 12, 0, 0)
    raw_artifact_id = uuid4()
    return FrozenMacroSnapshotRef(
        snapshot_id=uuid4(),
        series_id=uuid4(),
        snapshot_fingerprint=snapshot_fingerprint,
        payload_sha256=payload_sha256,
        fetched_at=fetched_at,
        series=FrozenMacroSeriesRef(
            provider_key="world_bank",
            source_id="2",
            external_indicator_id="SP.POP.TOTL",
            geography_type="country",
            geography_code="CHN",
            frequency="annual",
        ),
        snapshot=FrozenMacroSnapshotDetail(
            requested_country_code="CHN",
            query_start_year=2020,
            query_end_year=2024,
            source_id_snapshot="2",
            indicator_name="Population, total",
            indicator_unit="",
            source_name="World Development Indicators",
            source_note="Total population is based on the de facto definition.",
            source_organization="World Bank",
            provider_country_id="CHN",
            iso2_code="CN",
            iso3_code="CHN",
            geography_name="China",
            page=1,
            pages=1,
            per_page=50,
            provider_total=5,
            request_count=3,
            acquisition_method="official_api",
            authority_tier_snapshot=1,
            critical_claim_eligible_snapshot=True,
            fingerprint_version=1,
            normalization_version="world_bank_v1",
            status="available",
        ),
        observations=(
            FrozenMacroObservationRef(
                observation_id=uuid4(),
                period="2024",
                normalized_period_start=date(2024, 1, 1),
                value_numeric=Decimal("1410000000"),
                is_missing=False,
                decimal_scale=0,
                observation_status="",
                period_semantics="provider_year_label",
                frequency="annual",
            ),
        ),
        artifact_links=(
            FrozenMacroArtifactLinkRef(
                snapshot_artifact_id=uuid4(),
                artifact_id=raw_artifact_id,
                role="observations_page",
                page=1,
                response_status=200,
                final_hostname="api.worldbank.org",
                content_type="application/json",
                fetched_at=fetched_at,
            ),
        ),
        raw_artifacts=(
            FrozenMacroRawArtifactRef(
                artifact_id=raw_artifact_id,
                content_sha256="3" * 64,
                media_type="application/json",
                byte_size=128,
            ),
        ),
    )
