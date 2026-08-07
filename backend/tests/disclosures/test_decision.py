"""Decision logic tests: probe result -> access-mode verdict (strict invariants)."""

from datetime import datetime

import pytest

from app.disclosures.decision import decide_access_mode, is_auto_discoverable
from app.disclosures.probe import DisclosureAccessMode, DisclosureProbeResult


def _result(**overrides) -> DisclosureProbeResult:
    defaults: dict = {
        "provider_key": "sse",
        "checked_at": datetime(2026, 8, 7, 9, 0, 0),
        "access_mode": DisclosureAccessMode.UNAVAILABLE,
        "listing_page_reachable": False,
        "listing_results_visible_in_html": False,
        "direct_pdf_verified": False,
        "documented_api_found": False,
        "authentication_required": False,
        "request_count": 0,
        "notes": (),
        "listing_status_code": None,
        "final_hostname": None,
        "response_type": None,
        "search_request_applied": False,
        "matching_candidate_count": 0,
    }
    defaults.update(overrides)
    return DisclosureProbeResult(**defaults)


def test_documented_api_without_auth() -> None:
    assert (
        decide_access_mode(_result(documented_api_found=True))
        == DisclosureAccessMode.DOCUMENTED_API
    )


def test_documented_api_requiring_auth_is_auth_mode() -> None:
    assert (
        decide_access_mode(_result(documented_api_found=True, authentication_required=True))
        == DisclosureAccessMode.REQUIRES_AUTH_OR_CONTRACT
    )


def test_auth_signal_without_confirmed_api_is_not_auth_mode() -> None:
    # 仅页面出现登录/注册字样，但未确认官方 API 文档入口：不得判定需要认证。
    assert (
        decide_access_mode(_result(authentication_required=True))
        == DisclosureAccessMode.UNAVAILABLE
    )


def test_server_rendered_html_with_verified_pdf() -> None:
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                matching_candidate_count=1,
                direct_pdf_verified=True,
            )
        )
        == DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
    )


def test_matching_zero_never_server_rendered_html() -> None:
    # 不变量：matching_candidate_count=0 时不得返回 public_server_rendered_html。
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                matching_candidate_count=0,
                direct_pdf_verified=True,
            )
        )
        == DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    )


def test_no_pdf_never_direct_pdf_only() -> None:
    # 不变量：direct_pdf_verified=False 时不得返回 public_direct_pdf_only。
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                matching_candidate_count=2,
                direct_pdf_verified=False,
            )
        )
        == DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    )


def test_candidate_rows_but_no_direct_pdf_is_js_mode() -> None:
    # 有候选行但文件链接不是可直接验证的 PDF：需要详情页/JS/内部接口。
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                matching_candidate_count=1,
                direct_pdf_verified=False,
            )
        )
        == DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    )


def test_reachable_with_pdf_but_no_candidate_rows_is_direct_pdf_only() -> None:
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                matching_candidate_count=0,
                direct_pdf_verified=True,
            )
        )
        == DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    )


def test_reachable_shell_requiring_js_or_internal() -> None:
    assert (
        decide_access_mode(_result(listing_page_reachable=True, matching_candidate_count=0))
        == DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    )


def test_fully_unavailable() -> None:
    assert decide_access_mode(_result()) == DisclosureAccessMode.UNAVAILABLE


@pytest.mark.parametrize(
    "mode,auto",
    [
        (DisclosureAccessMode.DOCUMENTED_API, True),
        (DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML, True),
        (DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY, False),
        (DisclosureAccessMode.REQUIRES_AUTH_OR_CONTRACT, False),
        (DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT, False),
        (DisclosureAccessMode.UNAVAILABLE, False),
    ],
)
def test_is_auto_discoverable(mode: DisclosureAccessMode, auto: bool) -> None:
    assert is_auto_discoverable(mode) is auto
