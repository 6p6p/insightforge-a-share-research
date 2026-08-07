"""Decision logic: turn probe results into an access-mode verdict.

最终优先级（1—7，带显式不变量）：
1. 确认官方 API 文档入口且明确出现 API Key/App ID/access token/合同/订阅/申请权限
   → requires_auth_or_contract（只允许 documented_api_found=True 时触发认证判定）。
2. 确认官方 API 文档入口且无需认证 → documented_api。
3. 页面可达、search_request_applied=True、按公司/日期匹配到候选行、
   且候选直接解析为官方 PDF
   → public_server_rendered_html（不变量：search_request_applied=True、
   matching_candidate_count >= 1、direct_pdf_verified=True）。
4. 能确认官方 PDF 可公开下载 → public_direct_pdf_only
   （不变量：direct_pdf_verified 必须为 True）。
5. 页面可达但本次未把查询送入任何合规入口（search_request_applied=False）
   → discovery_not_confirmed。
6. 页面可达、查询已应用但未匹配到任何候选行（matching_candidate_count == 0）
   → discovery_not_confirmed。
7. 页面不可达且无任何通路证据 → unavailable。

绝对禁止：
- search_request_applied=False 时返回 public_server_rendered_html；
- matching_candidate_count=0 时返回 public_server_rendered_html；
- direct_pdf_verified=False 时返回 public_direct_pdf_only；
- 仅凭页面出现"登录/注册"就返回 requires_auth_or_contract；
- 当前阶段自动返回 requires_javascript_or_internal_endpoint——该形态必须由
  明确证据（如确认结果行需 JS 或仅内部接口可获取）支持，本阶段不自动推断。
"""

from app.disclosures.probe import DisclosureAccessMode, DisclosureProbeResult


def decide_access_mode(result: DisclosureProbeResult) -> DisclosureAccessMode:
    """根据单个 Provider 的探测结果给出接入形态结论。"""
    if result.documented_api_found:
        # 不变量：认证判定只允许在确认官方 API 文档入口后触发，
        # 且 authentication_required=True 时必须返回认证形态。
        if result.authentication_required:
            return DisclosureAccessMode.REQUIRES_AUTH_OR_CONTRACT
        return DisclosureAccessMode.DOCUMENTED_API
    if (
        result.listing_page_reachable
        and result.search_request_applied
        and result.matching_candidate_count >= 1
        and result.direct_pdf_verified
    ):
        # 不变量：server-rendered-html 必须同时满足 search_request_applied、
        # 候选非空与 PDF 已验证。
        return DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
    if result.direct_pdf_verified:
        # 能确认官方 PDF 公开下载、但未进入 server-rendered-html 分支
        # （未应用查询 / 候选行不可识别等）：只能确认公开下载，不能按公司/日期自动发现。
        return DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    if result.listing_page_reachable and not result.search_request_applied:
        # 页面可达但本次没有把查询送入任何合规入口：保守判为"未确认自动发现"，
        # 不推断需要 JS 或内部接口。
        return DisclosureAccessMode.DISCOVERY_NOT_CONFIRMED
    if (
        result.listing_page_reachable
        and result.search_request_applied
        and result.matching_candidate_count == 0
    ):
        # 页面可达、查询已应用但未匹配到候选行：同样保守判为"未确认"。
        return DisclosureAccessMode.DISCOVERY_NOT_CONFIRMED
    return DisclosureAccessMode.UNAVAILABLE


def is_auto_discoverable(access_mode: DisclosureAccessMode) -> bool:
    """接入形态是否支持自动化发现（不依赖隐藏接口/授权）。"""
    return access_mode in (
        DisclosureAccessMode.DOCUMENTED_API,
        DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML,
    )
