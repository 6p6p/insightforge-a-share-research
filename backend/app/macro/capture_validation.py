"""Captured macro fetch integrity validation (stage 2C.2B).

在持久化前校验一次捕获是否完整、可审计：元数据各恰好一条、观测分页
无缺/重/跳页、responses 数量与分页一致、hostname/content-type/source_id/
provider_key 与契约一致。失败抛 MacroCaptureInvalid，稳定消息不含
JSON body / 完整 query。
"""

from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.macro.capture import CapturedMacroFetch
from app.macro.errors import MacroCaptureInvalid

# 与 contracts._WDI_SOURCE_ID（"2"，World Development Indicators）保持一致。
_WDI_SOURCE_ID = "2"
_EXPECTED_HOSTNAME = "api.worldbank.org"
_MAX_OBSERVATION_PAGES = 18


def _base_media_type(content_type: str) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def validate_captured_macro_fetch(captured: CapturedMacroFetch) -> None:
    result = captured.result
    responses = captured.responses

    # 8. Provider 一致性：world_bank
    if result.provider_key != "world_bank":
        raise MacroCaptureInvalid("provider_key must be world_bank")
    # 9. source_id 必须为 WDI "2"
    if result.source_id != _WDI_SOURCE_ID:
        raise MacroCaptureInvalid("source_id must be 2")
    # 10. 观测分页上限
    if result.page_info.pages > _MAX_OBSERVATION_PAGES:
        raise MacroCaptureInvalid("pages exceeds observation page limit")

    indicator = [r for r in responses if r.role is MacroSnapshotArtifactRole.INDICATOR_METADATA]
    country = [r for r in responses if r.role is MacroSnapshotArtifactRole.COUNTRY_METADATA]
    observations = [r for r in responses if r.role is MacroSnapshotArtifactRole.OBSERVATIONS_PAGE]

    # 1. 正好一条 indicator_metadata
    if len(indicator) != 1:
        raise MacroCaptureInvalid("exactly one indicator_metadata response required")
    # 2. 正好一条 country_metadata
    if len(country) != 1:
        raise MacroCaptureInvalid("exactly one country_metadata response required")
    # 4. metadata page 必须为 None
    if indicator[0].page is not None or country[0].page is not None:
        raise MacroCaptureInvalid("metadata response page must be None")
    # 3. observations_page page 完整等于 1..pages（缺页/重复/page=0/跳页均拒绝）
    expected = list(range(1, result.page_info.pages + 1))
    actual = sorted(r.page for r in observations)
    if actual != expected:
        raise MacroCaptureInvalid("observations pages must be exactly 1..pages without gaps")
    # 5. responses 数量 = 2 + pages（request_count 可能因 redirect 更大，不作要求）
    if len(responses) != 2 + result.page_info.pages:
        raise MacroCaptureInvalid("response count must be 2 + pages")
    # 6/7/11. 每条 response：content-type base=json、hostname 固定、role/page 一致
    for response in responses:
        if _base_media_type(response.content_type) != "application/json":
            raise MacroCaptureInvalid("response content-type must be application/json")
        if response.final_hostname != _EXPECTED_HOSTNAME:
            raise MacroCaptureInvalid("unexpected final hostname")
        if response.role is MacroSnapshotArtifactRole.OBSERVATIONS_PAGE:
            page_ok = isinstance(response.page, int)
            if page_ok:
                page_ok = 1 <= response.page <= result.page_info.pages
            if not page_ok:
                raise MacroCaptureInvalid("observations_page role/page mismatch")
        elif response.page is not None:
            raise MacroCaptureInvalid("metadata role/page mismatch")
