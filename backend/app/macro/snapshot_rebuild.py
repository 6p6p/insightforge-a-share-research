"""Rebuild a macro snapshot fingerprint from persisted rows (stage 7B.1.1B).

`build_macro_snapshot_fingerprint` 的全部 semantic inputs 均已持久化到
MacroDatasetSnapshot / MacroSeries / MacroObservation /
MacroSnapshotArtifact / RawArtifact（审计结论见 7B.1.1B Part C/D）：

- series.* → MacroSeriesModel（稳定身份六字段）；
- query.* / provider_snapshot.* / indicator.* / geography.* / page_info.*
  → MacroDatasetSnapshotModel；
- observations[] → MacroObservationModel（value_numeric 为 plain Numeric，
  无 scale 补足问题；decimal_scale 由 value 重推）；
- raw_responses[] → MacroSnapshotArtifactModel + RawArtifact.content_sha256
  （content_type 用规范化常量 application/json）。

本模块只做「DB 行 → domain 契约」的重建，再调用 domain 公开 helper
`build_macro_snapshot_fingerprint` 重算 fingerprint；**不复制 fingerprint
算法本身**（canonical JSON / 排序 / Decimal str() 规则均在 fingerprint.py）。
"""

from uuid import UUID

from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel
from app.db.models.raw_artifact import RawArtifactModel
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
from app.macro.fingerprint import FingerprintArtifact, build_macro_snapshot_fingerprint

_FINGERPRINT_CONTENT_TYPE = "application/json"


def rebuild_macro_snapshot_fingerprint(
    snapshot: MacroDatasetSnapshotModel,
    series: MacroSeriesModel,
    observations: list[MacroObservationModel],
    links: list[MacroSnapshotArtifactModel],
    raw_artifacts: dict[UUID, RawArtifactModel],
) -> str:
    """从 persisted 行重算 macro snapshot fingerprint。

    重建 domain 契约（MacroFetchResult + FingerprintArtifact tuple）后调用
    `build_macro_snapshot_fingerprint`。若 persisted 数据违反 domain 不变量
    （如 source_id_snapshot != "2"），由契约 dataclass 抛 ValueError，调用方
    应将其包装为完整性错误。
    """
    result = _rebuild_result(snapshot, series, observations)
    fingerprint_artifacts = tuple(
        FingerprintArtifact(
            role=MacroSnapshotArtifactRole(link.role),
            page=link.page,
            sha256=raw_artifacts[link.artifact_id].content_sha256,
            response_status=link.response_status,
            final_hostname=link.final_hostname,
            content_type=_FINGERPRINT_CONTENT_TYPE,
        )
        for link in links
    )
    return build_macro_snapshot_fingerprint(result, fingerprint_artifacts)


def _rebuild_result(
    snapshot: MacroDatasetSnapshotModel,
    series: MacroSeriesModel,
    observations: list[MacroObservationModel],
) -> MacroFetchResult:
    """重建 MacroFetchResult（fetched_at / request_count 不参与 fingerprint 但契约要求）。

    顺序无关性由契约 __post_init__ 保证：observations 按
    (normalized_period_start, period) 重排、capabilities 按 value 重排。
    """
    query = MacroQuery(
        provider_key=series.provider_key,
        indicator_code=series.external_indicator_id,
        country_code=snapshot.requested_country_code,
        start_year=snapshot.query_start_year,
        end_year=snapshot.query_end_year,
    )
    indicator = MacroIndicator(
        provider_key=series.provider_key,
        external_indicator_id=series.external_indicator_id,
        name=snapshot.indicator_name,
        unit=snapshot.indicator_unit,
        source_id=snapshot.source_id_snapshot,
        source_name=snapshot.source_name,
        source_note=snapshot.source_note,
        source_organization=snapshot.source_organization,
        topics=tuple(
            MacroTopic(topic_id=topic["topic_id"], name=topic["name"])
            for topic in snapshot.topics_snapshot
        ),
    )
    geography = MacroGeography(
        geography_type=MacroGeographyType(series.geography_type),
        requested_code=snapshot.requested_country_code,
        provider_country_id=snapshot.provider_country_id,
        iso2_code=snapshot.iso2_code,
        iso3_code=snapshot.iso3_code,
        name=snapshot.geography_name,
        region_name=snapshot.region_name,
        income_level_name=snapshot.income_level_name,
    )
    page_info = MacroPageInfo(
        page=snapshot.page,
        pages=snapshot.pages,
        per_page=snapshot.per_page,
        total=snapshot.provider_total,
        last_updated=snapshot.provider_last_updated,
    )
    macro_observations = tuple(
        MacroObservation(
            provider_key=series.provider_key,
            external_indicator_id=series.external_indicator_id,
            geography_code=snapshot.iso3_code,
            period=observation.period,
            normalized_period_start=observation.normalized_period_start,
            frequency=MacroFrequency(observation.frequency),
            value=observation.value_numeric,
            is_missing=observation.is_missing,
            period_semantics=MacroPeriodSemantics(observation.period_semantics),
            observation_status=observation.observation_status,
            decimal_scale=observation.decimal_scale,
        )
        for observation in observations
    )
    return MacroFetchResult(
        provider_key=series.provider_key,
        query=query,
        indicator=indicator,
        geography=geography,
        observations=macro_observations,
        page_info=page_info,
        fetched_at=snapshot.fetched_at,
        request_count=snapshot.request_count,
        acquisition_method=AcquisitionMethod(snapshot.acquisition_method),
        authority_tier=SourceAuthorityTier(snapshot.authority_tier_snapshot),
        critical_claim_eligible=snapshot.critical_claim_eligible_snapshot,
        provider_capabilities=tuple(
            SourceCapability(cap) for cap in snapshot.provider_capabilities_snapshot
        ),
        source_id=snapshot.source_id_snapshot,
    )
