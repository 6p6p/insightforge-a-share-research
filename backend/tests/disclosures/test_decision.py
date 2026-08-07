"""Decision logic tests: probe result -> access-mode verdict."""

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


def test_server_rendered_html_with_verified_pdf() -> None:
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                listing_results_visible_in_html=True,
                direct_pdf_verified=True,
            )
        )
        == DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
    )


def test_results_visible_but_no_pdf_is_direct_pdf_only() -> None:
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                listing_results_visible_in_html=True,
                direct_pdf_verified=False,
            )
        )
        == DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    )


def test_reachable_with_pdf_but_no_results_rows() -> None:
    assert (
        decide_access_mode(
            _result(
                listing_page_reachable=True,
                listing_results_visible_in_html=False,
                direct_pdf_verified=True,
            )
        )
        == DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    )


def test_reachable_shell_requiring_js_or_internal() -> None:
    assert (
        decide_access_mode(
            _result(listing_page_reachable=True, listing_results_visible_in_html=False)
        )
        == DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    )


def test_unreachable_with_auth_signal() -> None:
    assert (
        decide_access_mode(_result(authentication_required=True))
        == DisclosureAccessMode.REQUIRES_AUTH_OR_CONTRACT
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
