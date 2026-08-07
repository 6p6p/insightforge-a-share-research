"""Feasibility probe model for official disclosure sources.

探测结果只描述"公开通路形态"，不保存 HTML 正文、完整 query 或响应正文；
notes 使用稳定代码，便于跨 Provider 比对与后续决策。
"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class DisclosureAccessMode(StrEnum):
    """官方披露公开接入形态（用于决定是否值得实现自动发现）。"""

    DOCUMENTED_API = "documented_api"
    PUBLIC_SERVER_RENDERED_HTML = "public_server_rendered_html"
    PUBLIC_DIRECT_PDF_ONLY = "public_direct_pdf_only"
    REQUIRES_AUTH_OR_CONTRACT = "requires_auth_or_contract"
    REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT = "requires_javascript_or_internal_endpoint"
    UNAVAILABLE = "unavailable"


# 稳定 note 代码，避免在结果里保存完整 HTML / query / 响应正文。
NOTE_PUBLIC_SEARCH_FORM_FOUND = "public_search_form_found"
NOTE_RESULT_ROWS_NOT_IN_INITIAL_HTML = "result_rows_not_in_initial_html"
NOTE_OFFICIAL_PDF_LINK_VERIFIED = "official_pdf_link_verified"
NOTE_DOCUMENTED_API_REQUIRES_REGISTRATION = "documented_api_requires_registration"
NOTE_NO_PUBLIC_API_DOCUMENTATION_CONFIRMED = "no_public_api_documentation_confirmed"
NOTE_PROBE_HTTP_ERROR = "probe_http_error"
NOTE_PROBE_TIMEOUT = "probe_timeout"
NOTE_REDIRECT_VIOLATED_ALLOWLIST = "redirect_violated_allowlist"
NOTE_HTML_OVER_LIMIT = "html_over_limit"


@dataclass(frozen=True)
class DisclosureProbeResult:
    provider_key: str
    checked_at: datetime
    access_mode: DisclosureAccessMode
    listing_page_reachable: bool
    listing_results_visible_in_html: bool
    direct_pdf_verified: bool
    documented_api_found: bool
    authentication_required: bool
    request_count: int
    notes: tuple[str, ...] = ()
