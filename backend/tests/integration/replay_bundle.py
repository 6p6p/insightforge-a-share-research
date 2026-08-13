"""Replay bundle builder（stage 7B.1.4B.1 测试共享）。

与 tests/eval/conftest.py 的 `build_bundle` 不同：这里 document 是 **有效 HTML**
（media_type=text/html），可被 `LocalRawArtifactStore.put_html_bytes` 落盘，也能被
`SourceParsingService.parse_html_bytes` 解析 + `ChunkingService` 分块，从而支撑
rehydrate → parse → chunk 的端到端证明。

`doc_provider_key` / `doc_media_type` 可参数化，供 tamper / 负例测试构造自洽但
语义不一致的 bundle（snapshot fingerprint 始终与 case 引用闭合）。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.domain.sources import AcquisitionMethod, SourceAuthorityTier, SourceCapability
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.canonical import canonical_json_bytes
from app.eval.contracts import (
    EvalCase,
    EvalDatasetCaseRef,
    EvalDatasetManifest,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenMacroArtifactLinkRef,
    FrozenMacroObservationRef,
    FrozenMacroRawArtifactRef,
    FrozenMacroSeriesRef,
    FrozenMacroSnapshotDetail,
    FrozenMacroSnapshotRef,
    FrozenMacroTopicRef,
    FrozenSourceProviderRef,
    FrozenSourceSnapshot,
)
from app.eval.fingerprints import (
    compute_eval_case_fingerprint,
    compute_source_snapshot_fingerprint,
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

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
SOURCE_RECORD_ID = UUID("22222222-2222-2222-2222-222222222222")
RAW_ARTIFACT_ID = UUID("33333333-3333-3333-3333-333333333333")
CASE_ID = "test-replay-fundamentals"
CASE_VERSION = 1
DATASET_ID = "insightforge_eval_replay_test"

# 与 materializer 测试的 _doc_html 同构，保证 parse_html_bytes 能产生 >=1 个 block。
DOC_HTML = (
    "<html><head><title>研究新闻</title></head><body><article>"
    "<p>2024年贵州茅台营业收入123,456万元，净利润同比增长。</p>"
    "</article></body></html>"
).encode()
DOC_SHA256 = hashlib.sha256(DOC_HTML).hexdigest()

_ACQUIRED_AT = datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC)
_PUBLISHED_AT = datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC)


@dataclass(frozen=True)
class ReplayBundleSpec:
    manifest: EvalDatasetManifest
    case: EvalCase
    snapshot: FrozenSourceSnapshot
    document_content: bytes
    document_sha256: str
    snapshot_fingerprint: str


def build_replay_bundle(
    root: str | Path,
    *,
    doc_provider_key: str = "xinhuanet",
    doc_media_type: str = "text/html",
    company_exchange: str = "SSE",
) -> ReplayBundleSpec:
    """写入一个可 rehydrate 的 bundle，返回其 frozen spec（确定性）。

    `company_exchange` 用于构造「通过 frozen 契约但违反目标 schema CHECK」的
    case（如 exchange='NASDAQ'），触发 rehydrator 的 `EvalReplayIntegrityError`。
    """
    doc = FrozenDocumentSourceRef(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=DOC_SHA256,
        provider_key=doc_provider_key,
        document_type="news_article",
        media_type=doc_media_type,
        title="研究新闻",
        source_url="https://www.xinhuanet.com/2026/0809/0001.htm",
        acquired_at=_ACQUIRED_AT,
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=False,
        published_at=_PUBLISHED_AT,
    )
    providers = (
        FrozenSourceProviderRef(
            provider_key="xinhuanet",
            display_name="新华网",
            enabled=True,
            capabilities=("news_article",),
        ),
        FrozenSourceProviderRef(
            provider_key="sse",
            display_name="上海证券交易所",
            enabled=True,
            capabilities=("company_announcement", "document_download"),
        ),
    )
    snapshot = FrozenSourceSnapshot(
        document_sources=(doc,),
        source_providers=providers,
    )
    snapshot_fp = compute_source_snapshot_fingerprint(snapshot)
    case = EvalCase(
        case_id=CASE_ID,
        case_version=CASE_VERSION,
        company_id=COMPANY_ID,
        company=FrozenCompanyIdentity(
            security_code="600519",
            official_name="公司600519",
            short_name="600519",
            exchange=company_exchange,
            board="sse_main",
            aliases=("贵州茅台", "茅台股份"),
        ),
        research_question="贵州茅台 2024 年基本面是否支撑当前估值？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        source_snapshot_fingerprint=snapshot_fp,
    )
    case_fp = compute_eval_case_fingerprint(case)
    manifest = EvalDatasetManifest(
        dataset_id=DATASET_ID,
        dataset_version=1,
        cases=(
            EvalDatasetCaseRef(
                case_id=CASE_ID,
                case_version=CASE_VERSION,
                case_fingerprint=case_fp,
            ),
        ),
    )

    writer = EvaluationBundleWriter(root)
    writer.write_document_blob(DOC_SHA256, DOC_HTML)
    writer.write_snapshot(snapshot)
    writer.write_case(case)
    writer.write_manifest(manifest)

    return ReplayBundleSpec(
        manifest=manifest,
        case=case,
        snapshot=snapshot,
        document_content=DOC_HTML,
        document_sha256=DOC_SHA256,
        snapshot_fingerprint=snapshot_fp,
    )


# ---------------------------------------------------------------- macro replay bundle

MACRO_CASE_ID = "test-replay-macro-fundamentals"
MACRO_CASE_VERSION = 1
MACRO_SERIES_ID = UUID("44444444-4444-4444-4444-444444444444")
MACRO_SNAPSHOT_ID = UUID("55555555-5555-5555-5555-555555555555")
MACRO_INDICATOR_ARTIFACT_ID = UUID("66666666-6666-6666-6666-666666666666")
MACRO_COUNTRY_ARTIFACT_ID = UUID("77777777-7777-7777-7777-777777777777")
MACRO_OBSERVATIONS_ARTIFACT_ID = UUID("88888888-8888-8888-8888-888888888888")
MACRO_INDICATOR_LINK_ID = UUID("99999999-9999-9999-9999-999999999901")
MACRO_COUNTRY_LINK_ID = UUID("99999999-9999-9999-9999-999999999902")
MACRO_OBSERVATIONS_LINK_ID = UUID("99999999-9999-9999-9999-999999999903")
MACRO_OBSERVATION_IDS = (
    UUID("99999999-9999-9999-9999-999999999911"),
    UUID("99999999-9999-9999-9999-999999999912"),
    UUID("99999999-9999-9999-9999-999999999913"),
    UUID("99999999-9999-9999-9999-999999999914"),
    UUID("99999999-9999-9999-9999-999999999915"),
)

_MACRO_FETCHED_AT = datetime(2026, 8, 1, 13, 0, 0, tzinfo=UTC)
_MACRO_HOSTNAME = "api.worldbank.org"
_MACRO_CONTENT_TYPE = "application/json"
_MACRO_INDICATOR = "SP.POP.TOTL"
_MACRO_COUNTRY = "CHN"
_MACRO_YEARS = (2020, 2021, 2022, 2023, 2024)
_MACRO_VALUE = Decimal("1410000000")

_MACRO_INDICATOR_BYTES = json.dumps(
    {"id": _MACRO_INDICATOR, "name": "Population, total"}, sort_keys=True
).encode("utf-8")
_MACRO_COUNTRY_BYTES = json.dumps({"id": _MACRO_COUNTRY, "name": "China"}, sort_keys=True).encode(
    "utf-8"
)
_MACRO_OBSERVATIONS_BYTES = json.dumps(
    {"indicator": _MACRO_INDICATOR, "rows": list(_MACRO_YEARS)}, sort_keys=True
).encode("utf-8")

# role → (raw artifact_id, snapshot_artifact_id, page, raw bytes)
_MACRO_ROLE_ROWS = (
    (
        MacroSnapshotArtifactRole.INDICATOR_METADATA,
        MACRO_INDICATOR_ARTIFACT_ID,
        MACRO_INDICATOR_LINK_ID,
        None,
        _MACRO_INDICATOR_BYTES,
    ),
    (
        MacroSnapshotArtifactRole.COUNTRY_METADATA,
        MACRO_COUNTRY_ARTIFACT_ID,
        MACRO_COUNTRY_LINK_ID,
        None,
        _MACRO_COUNTRY_BYTES,
    ),
    (
        MacroSnapshotArtifactRole.OBSERVATIONS_PAGE,
        MACRO_OBSERVATIONS_ARTIFACT_ID,
        MACRO_OBSERVATIONS_LINK_ID,
        1,
        _MACRO_OBSERVATIONS_BYTES,
    ),
)


def _macro_fetch_result() -> MacroFetchResult:
    query = MacroQuery(
        provider_key="world_bank",
        indicator_code=_MACRO_INDICATOR,
        country_code=_MACRO_COUNTRY,
        start_year=_MACRO_YEARS[0],
        end_year=_MACRO_YEARS[-1],
    )
    indicator = MacroIndicator(
        provider_key="world_bank",
        external_indicator_id=_MACRO_INDICATOR,
        name="Population, total",
        unit="",
        source_id="2",
        source_name="World Development Indicators",
        source_note="Total population is based on the de facto definition.",
        source_organization="World Bank",
        topics=(MacroTopic(topic_id="19", name="Population: Structure, growth & density"),),
    )
    geography = MacroGeography(
        geography_type=MacroGeographyType.COUNTRY,
        requested_code=_MACRO_COUNTRY,
        provider_country_id=_MACRO_COUNTRY,
        iso2_code="CN",
        iso3_code=_MACRO_COUNTRY,
        name="China",
        region_name="East Asia & Pacific",
        income_level_name="Upper middle income",
    )
    observations = tuple(
        MacroObservation(
            provider_key="world_bank",
            external_indicator_id=_MACRO_INDICATOR,
            geography_code=_MACRO_COUNTRY,
            period=str(year),
            normalized_period_start=date(year, 1, 1),
            frequency=MacroFrequency.ANNUAL,
            value=_MACRO_VALUE,
            is_missing=False,
            period_semantics=MacroPeriodSemantics.PROVIDER_YEAR_LABEL,
            observation_status="",
        )
        for year in _MACRO_YEARS
    )
    return MacroFetchResult(
        provider_key="world_bank",
        query=query,
        indicator=indicator,
        geography=geography,
        observations=observations,
        page_info=MacroPageInfo(
            page=1,
            pages=1,
            per_page=50,
            total=len(observations),
            last_updated="2026-01-01",
        ),
        fetched_at=_MACRO_FETCHED_AT,
        request_count=3,
        acquisition_method=AcquisitionMethod.OFFICIAL_API,
        authority_tier=SourceAuthorityTier.TIER_1,
        critical_claim_eligible=True,
        provider_capabilities=(SourceCapability.DOCUMENT_DOWNLOAD, SourceCapability.MACRO_DATA),
    )


def build_macro_replay_bundle(root: str | Path) -> ReplayBundleSpec:
    """写入一个含 macro closure 的 replay bundle（document + world_bank macro）。

    与 `build_replay_bundle` 的区别：source_providers 额外含 `world_bank`，snapshot
    携带一个自洽的 macro closure（series / snapshot 行 / observations / artifact_links
    / raw_artifacts）。`snapshot_fingerprint` 用 domain `build_macro_snapshot_fingerprint`
    重算（不复制算法），rehydrate 后由 `verify_snapshot_integrity` 证明一致。
    """
    result = _macro_fetch_result()

    fingerprint_artifacts = tuple(
        FingerprintArtifact(
            role=role,
            page=page,
            sha256=hashlib.sha256(blob).hexdigest(),
            response_status=200,
            final_hostname=_MACRO_HOSTNAME,
            content_type=_MACRO_CONTENT_TYPE,
        )
        for role, _artifact_id, _link_id, page, blob in _MACRO_ROLE_ROWS
    )
    macro_fingerprint = build_macro_snapshot_fingerprint(result, fingerprint_artifacts)

    series_ref = FrozenMacroSeriesRef(
        provider_key="world_bank",
        source_id=result.source_id,
        external_indicator_id=result.indicator.external_indicator_id,
        geography_type=result.geography.geography_type.value,
        geography_code=result.geography.iso3_code,
        frequency=MacroFrequency.ANNUAL.value,
    )
    detail = FrozenMacroSnapshotDetail(
        requested_country_code=result.query.country_code,
        query_start_year=result.query.start_year,
        query_end_year=result.query.end_year,
        source_id_snapshot=result.source_id,
        indicator_name=result.indicator.name,
        indicator_unit=result.indicator.unit,
        source_name=result.indicator.source_name,
        source_note=result.indicator.source_note,
        source_organization=result.indicator.source_organization,
        topics_snapshot=tuple(
            FrozenMacroTopicRef(topic_id=t.topic_id, name=t.name) for t in result.indicator.topics
        ),
        provider_country_id=result.geography.provider_country_id,
        iso2_code=result.geography.iso2_code,
        iso3_code=result.geography.iso3_code,
        geography_name=result.geography.name,
        region_name=result.geography.region_name,
        income_level_name=result.geography.income_level_name,
        page=result.page_info.page,
        pages=result.page_info.pages,
        per_page=result.page_info.per_page,
        provider_total=result.page_info.total,
        provider_last_updated=result.page_info.last_updated,
        request_count=result.request_count,
        acquisition_method=result.acquisition_method.value,
        authority_tier_snapshot=int(result.authority_tier),
        critical_claim_eligible_snapshot=result.critical_claim_eligible,
        provider_capabilities_snapshot=tuple(c.value for c in result.provider_capabilities),
    )
    observation_refs = tuple(
        FrozenMacroObservationRef(
            observation_id=MACRO_OBSERVATION_IDS[i],
            period=o.period,
            normalized_period_start=o.normalized_period_start,
            value_numeric=o.value,
            is_missing=o.is_missing,
            decimal_scale=o.decimal_scale,
            observation_status=o.observation_status,
        )
        for i, o in enumerate(result.observations)
    )
    link_refs = tuple(
        FrozenMacroArtifactLinkRef(
            snapshot_artifact_id=link_id,
            artifact_id=artifact_id,
            role=role.value,
            page=page,
            response_status=200,
            final_hostname=_MACRO_HOSTNAME,
            content_type=_MACRO_CONTENT_TYPE,
            fetched_at=_MACRO_FETCHED_AT,
        )
        for role, artifact_id, link_id, page, _blob in _MACRO_ROLE_ROWS
    )
    raw_refs = tuple(
        FrozenMacroRawArtifactRef(
            artifact_id=artifact_id,
            content_sha256=hashlib.sha256(blob).hexdigest(),
            media_type=_MACRO_CONTENT_TYPE,
            byte_size=len(blob),
            role=role.value,
        )
        for role, artifact_id, _link_id, _page, blob in _MACRO_ROLE_ROWS
    )
    payload = {
        "schema_version": 1,
        "snapshot_fingerprint": macro_fingerprint,
        "fetched_at": result.fetched_at.isoformat(),
        "series": {
            "provider_key": series_ref.provider_key,
            "source_id": series_ref.source_id,
            "external_indicator_id": series_ref.external_indicator_id,
            "geography_type": series_ref.geography_type,
            "geography_code": series_ref.geography_code,
            "frequency": series_ref.frequency,
        },
        "indicator": {
            "name": detail.indicator_name,
            "unit": detail.indicator_unit,
            "source_name": detail.source_name,
            "source_note": detail.source_note,
            "source_organization": detail.source_organization,
            "topics": [{"topic_id": t.topic_id, "name": t.name} for t in detail.topics_snapshot],
        },
        "geography": {
            "provider_country_id": detail.provider_country_id,
            "iso2_code": detail.iso2_code,
            "iso3_code": detail.iso3_code,
            "name": detail.geography_name,
            "region_name": detail.region_name,
            "income_level_name": detail.income_level_name,
        },
        "observations": [
            {
                "period": o.period,
                "normalized_period_start": o.normalized_period_start.isoformat(),
                "value": str(o.value_numeric),
                "is_missing": o.is_missing,
                "decimal_scale": o.decimal_scale,
                "observation_status": o.observation_status,
            }
            for o in observation_refs
        ],
    }
    payload_sha = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    macro_ref = FrozenMacroSnapshotRef(
        snapshot_id=MACRO_SNAPSHOT_ID,
        series_id=MACRO_SERIES_ID,
        snapshot_fingerprint=macro_fingerprint,
        payload_sha256=payload_sha,
        fetched_at=result.fetched_at,
        series=series_ref,
        snapshot=detail,
        observations=observation_refs,
        artifact_links=link_refs,
        raw_artifacts=raw_refs,
    )

    doc = FrozenDocumentSourceRef(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=DOC_SHA256,
        provider_key="xinhuanet",
        document_type="news_article",
        media_type="text/html",
        title="研究新闻",
        source_url="https://www.xinhuanet.com/2026/0809/0001.htm",
        acquired_at=_ACQUIRED_AT,
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=False,
        published_at=_PUBLISHED_AT,
    )
    providers = (
        FrozenSourceProviderRef(
            provider_key="xinhuanet",
            display_name="新华网",
            enabled=True,
            capabilities=("news_article",),
        ),
        FrozenSourceProviderRef(
            provider_key="sse",
            display_name="上海证券交易所",
            enabled=True,
            capabilities=("company_announcement", "document_download"),
        ),
        FrozenSourceProviderRef(
            provider_key="world_bank",
            display_name="World Bank Open Data",
            enabled=True,
            capabilities=("macro_data", "document_download"),
        ),
    )
    snapshot = FrozenSourceSnapshot(
        document_sources=(doc,),
        macro_snapshots=(macro_ref,),
        source_providers=providers,
    )
    snapshot_fp = compute_source_snapshot_fingerprint(snapshot)
    case = EvalCase(
        case_id=MACRO_CASE_ID,
        case_version=MACRO_CASE_VERSION,
        company_id=COMPANY_ID,
        company=FrozenCompanyIdentity(
            security_code="600519",
            official_name="公司600519",
            short_name="600519",
            exchange="SSE",
            board="sse_main",
            aliases=("贵州茅台", "茅台股份"),
        ),
        research_question="贵州茅台 2024 年基本面是否支撑当前估值？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0, tzinfo=UTC),
        source_snapshot_fingerprint=snapshot_fp,
    )
    case_fp = compute_eval_case_fingerprint(case)
    manifest = EvalDatasetManifest(
        dataset_id=DATASET_ID,
        dataset_version=1,
        cases=(
            EvalDatasetCaseRef(
                case_id=MACRO_CASE_ID,
                case_version=MACRO_CASE_VERSION,
                case_fingerprint=case_fp,
            ),
        ),
    )

    writer = EvaluationBundleWriter(root)
    writer.write_document_blob(DOC_SHA256, DOC_HTML)
    for _role, _artifact_id, _link_id, _page, blob in _MACRO_ROLE_ROWS:
        writer.write_document_blob(hashlib.sha256(blob).hexdigest(), blob)
    writer.write_snapshot(snapshot)
    writer.write_case(case)
    writer.write_manifest(manifest)
    writer.write_macro_payload(macro_ref, payload)

    return ReplayBundleSpec(
        manifest=manifest,
        case=case,
        snapshot=snapshot,
        document_content=DOC_HTML,
        document_sha256=DOC_SHA256,
        snapshot_fingerprint=snapshot_fp,
    )
