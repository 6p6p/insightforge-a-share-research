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
NOTE_OFFICIAL_DATA_SERVICE_LINK_FOUND = "official_data_service_link_found"
NOTE_API_ACCESS_TERMS_NOT_VERIFIED = "api_access_terms_not_verified"
NOTE_NO_PUBLIC_API_DOCUMENTATION_CONFIRMED = "no_public_api_documentation_confirmed"
NOTE_PROBE_HTTP_ERROR = "probe_http_error"
NOTE_PROBE_TIMEOUT = "probe_timeout"
NOTE_REDIRECT_VIOLATED_ALLOWLIST = "redirect_violated_allowlist"
NOTE_HTML_OVER_LIMIT = "html_over_limit"
NOTE_SEARCH_REQUEST_NOT_APPLIED = "search_request_not_applied"
NOTE_NO_MATCHING_CANDIDATE_ROW = "no_matching_candidate_row"


@dataclass(frozen=True)
class DisclosureProbeResult:
    """单次探测结果：只描述"公开通路形态"，可审计但不泄露正文/query。

    - 不保存 URL、完整 query、响应正文；
    - final_hostname 只保存 hostname（不含路径与 query）；
    - matching_candidate_count 必须非负；
    - search_request_applied=False 时不得声称已按公司/日期筛选。
    """

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
    listing_status_code: int | None = None
    final_hostname: str | None = None
    response_type: str | None = None
    search_request_applied: bool = False
    matching_candidate_count: int = 0

    def __post_init__(self) -> None:
        if self.request_count < 0:
            raise ValueError("request_count 不能为负")
        if self.matching_candidate_count < 0:
            raise ValueError("matching_candidate_count 不能为负")
