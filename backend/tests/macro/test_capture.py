"""Unit tests for raw JSON capture contract and capture validation (stage 2C.2B).

覆盖：
- MacroRawJsonResponse 8 项 __post_init__ 校验（role/page/status/hostname/
  content-type/raw_bytes 上限/时区感知）；
- CapturedMacroFetch 结构校验；
- validate_captured_macro_fetch 的 11 项完整性校验（缺页/重复/跳页/
  hostname/content-type/source_id/provider_key/pages 上限）；
- 错误分类：4 类 MacroPersistenceError 的稳定 code，消息不含敏感信息。
"""

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)
from app.macro.capture import CapturedMacroFetch, MacroRawJsonResponse
from app.macro.capture_validation import validate_captured_macro_fetch
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
)
from app.macro.errors import (
    MacroArtifactConflict,
    MacroCaptureInvalid,
    MacroPersistenceFailed,
    MacroSnapshotIntegrityError,
)

_FETCHED_AT = datetime(2026, 1, 1, tzinfo=UTC)
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
    source_note="",
    source_organization="World Bank",
    topics=(),
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


def _observation(year: int, value: object = 1410000000) -> MacroObservation:
    return MacroObservation(
        provider_key="world_bank",
        external_indicator_id="SP.POP.TOTL",
        geography_code="CHN",
        period=str(year),
        normalized_period_start=date(year, 1, 1),
        frequency=MacroFrequency.ANNUAL,
        value=Decimal(str(value)),
        is_missing=False,
        period_semantics=MacroPeriodSemantics.PROVIDER_YEAR_LABEL,
        observation_status="",
    )


def _result(*, pages: int = 1) -> MacroFetchResult:
    observations = tuple(_observation(y) for y in range(2020, 2025))
    return MacroFetchResult(
        provider_key="world_bank",
        query=_QUERY,
        indicator=_INDICATOR,
        geography=_GEOGRAPHY,
        observations=observations,
        page_info=MacroPageInfo(page=1, pages=pages, per_page=1000, total=5),
        fetched_at=_FETCHED_AT,
        request_count=3,
        acquisition_method=AcquisitionMethod.OFFICIAL_API,
        authority_tier=SourceAuthorityTier.TIER_1,
        critical_claim_eligible=True,
        provider_capabilities=(SourceCapability.DOCUMENT_DOWNLOAD, SourceCapability.MACRO_DATA),
    )


def _raw(
    *,
    role: MacroSnapshotArtifactRole = MacroSnapshotArtifactRole.INDICATOR_METADATA,
    page: int | None = None,
    status: int = 200,
    hostname: str = "api.worldbank.org",
    content_type: str = "application/json",
    raw_bytes: bytes = b'{"ok": true}',
    fetched_at: datetime = _FETCHED_AT,
) -> MacroRawJsonResponse:
    return MacroRawJsonResponse(
        role=role,
        page=page,
        response_status=status,
        final_hostname=hostname,
        content_type=content_type,
        fetched_at=fetched_at,
        raw_bytes=raw_bytes,
    )


def _captured(
    *,
    result: MacroFetchResult | None = None,
    responses: list[MacroRawJsonResponse] | None = None,
) -> CapturedMacroFetch:
    if result is None:
        result = _result()
    if responses is None:
        responses = [
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
        ]
    return CapturedMacroFetch(result=result, responses=tuple(responses))


# ---------------------------------------------------- MacroRawJsonResponse


def test_raw_response_accepts_valid() -> None:
    _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1)


def test_raw_response_requires_known_role() -> None:
    with pytest.raises(ValueError):
        _raw(role="nope")  # type: ignore[arg-type]


def test_raw_response_metadata_page_must_be_none() -> None:
    with pytest.raises(ValueError):
        _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA, page=1)
    with pytest.raises(ValueError):
        _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA, page=1)


def test_raw_response_observations_page_requires_positive_int() -> None:
    with pytest.raises(ValueError):
        _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=0)
    with pytest.raises(ValueError):
        _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=None)


def test_raw_response_requires_2xx_status() -> None:
    with pytest.raises(ValueError):
        _raw(status=301)
    with pytest.raises(ValueError):
        _raw(status=404)
    with pytest.raises(ValueError):
        _raw(status=199)


def test_raw_response_requires_bare_hostname() -> None:
    with pytest.raises(ValueError):
        _raw(hostname="https://api.worldbank.org")
    with pytest.raises(ValueError):
        _raw(hostname="api.worldbank.org/foo")
    with pytest.raises(ValueError):
        _raw(hostname="")
    with pytest.raises(ValueError):
        _raw(hostname="bad host!")


def test_raw_response_requires_json_content_type() -> None:
    with pytest.raises(ValueError):
        _raw(content_type="text/html")


def test_raw_response_requires_nonempty_bytes_within_limit() -> None:
    with pytest.raises(ValueError):
        _raw(raw_bytes=b"")
    with pytest.raises(ValueError):
        _raw(raw_bytes=b"x" * (5 * 1024 * 1024 + 1))


def test_raw_response_requires_aware_datetime() -> None:
    with pytest.raises(ValueError):
        _raw(fetched_at=datetime(2026, 1, 1))


def test_captured_fetch_rejects_non_tuple_responses() -> None:
    with pytest.raises(ValueError):
        CapturedMacroFetch(result=_result(), responses=[_raw()])  # type: ignore[arg-type]


# ---------------------------------------------------- validate_captured_macro_fetch


def test_validation_accepts_complete_capture() -> None:
    validate_captured_macro_fetch(_captured())


def test_validation_rejects_missing_metadata() -> None:
    captured = _captured(
        responses=[
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
        ]
    )
    with pytest.raises(MacroCaptureInvalid):
        validate_captured_macro_fetch(captured)


def test_validation_rejects_duplicate_metadata() -> None:
    captured = _captured(
        responses=[
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
        ]
    )
    with pytest.raises(MacroCaptureInvalid):
        validate_captured_macro_fetch(captured)


def test_validation_rejects_gapped_observation_pages() -> None:
    result = _result(pages=2)
    captured = _captured(
        result=result,
        responses=[
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
            # 缺 page=2
        ],
    )
    with pytest.raises(MacroCaptureInvalid):
        validate_captured_macro_fetch(captured)


def test_validation_rejects_wrong_response_count() -> None:
    captured = _captured(
        responses=[
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
        ]
    )
    with pytest.raises(MacroCaptureInvalid):
        validate_captured_macro_fetch(captured)


def test_validation_rejects_unexpected_hostname() -> None:
    captured = _captured(
        responses=[
            _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
            _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA, hostname="evil.example"),
            _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=1),
        ]
    )
    with pytest.raises(MacroCaptureInvalid):
        validate_captured_macro_fetch(captured)


def test_validation_rejects_too_many_pages() -> None:
    result = _result(pages=19)
    responses = [
        _raw(role=MacroSnapshotArtifactRole.INDICATOR_METADATA),
        _raw(role=MacroSnapshotArtifactRole.COUNTRY_METADATA),
    ]
    responses += [
        _raw(role=MacroSnapshotArtifactRole.OBSERVATIONS_PAGE, page=p) for p in range(1, 20)
    ]
    with pytest.raises(MacroCaptureInvalid):
        validate_captured_macro_fetch(_captured(result=result, responses=responses))


# ---------------------------------------------------- error taxonomy (§十四)


def test_error_codes_stable() -> None:
    assert MacroCaptureInvalid("x").code == "macro_capture_invalid"
    assert MacroArtifactConflict("x").code == "macro_artifact_conflict"
    assert MacroSnapshotIntegrityError("x").code == "macro_snapshot_integrity_error"
    assert MacroPersistenceFailed("x").code == "macro_persistence_failed"


def test_error_messages_do_not_leak_sensitive_values() -> None:
    # 错误消息不得含 raw JSON body / storage 绝对路径 / DB URL / 完整 URL / 域名全集。
    leaks = ("http://", "https://", ":\\", "C:\\", "password", "api_key", '{"')
    for exc in (
        MacroCaptureInvalid("macro capture invalid"),
        MacroArtifactConflict("macro artifact conflict"),
        MacroSnapshotIntegrityError("macro snapshot integrity error"),
        MacroPersistenceFailed("macro persistence failed"),
    ):
        assert str(exc) not in ("",)
        for marker in leaks:
            assert marker not in str(exc), f"{type(exc).__name__} leaks {marker!r}"
