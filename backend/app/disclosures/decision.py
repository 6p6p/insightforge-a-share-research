"""Decision logic: turn probe results into an access-mode verdict.

优先级（1—6，带显式不变量）：
1. 确认官方 API 文档入口且明确出现 API Key/App ID/access token/合同/订阅/申请权限
   → requires_auth_or_contract（只允许 documented_api_found=True 时触发认证判定）。
2. 确认官方 API 文档入口且无需认证 → documented_api。
3. 页面可达、按公司/日期匹配到候选行、且首个候选直接解析为官方 PDF
   → public_server_rendered_html（不变量：matching_candidate_count >= 1）。
4. 能确认官方 PDF 可公开下载、但无法按公司匹配到候选行
   → public_direct_pdf_only（不变量：direct_pdf_verified 必须为 True）。
5. 页面可达但候选行不可识别 / 需要 JS 或未公开接口 → requires_javascript_or_internal_endpoint
   （不变量：matching_candidate_count == 0）。
6. 其余（页面不可达且无任何通路证据）→ unavailable。

绝对禁止：
- direct_pdf_verified=False 时返回 public_direct_pdf_only；
- matching_candidate_count=0 时返回 public_server_rendered_html；
- 仅凭页面出现"登录/注册"就返回 requires_auth_or_contract。
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
    if result.listing_page_reachable and result.matching_candidate_count >= 1:
        if result.direct_pdf_verified:
            return DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
        # 有候选行但文件链接不是可直接验证的 PDF：仍需详情页/JS/内部接口。
        return DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    if result.direct_pdf_verified:
        # 能确认官方 PDF 公开下载、但未进入 server-rendered-html 分支
        # （候选行不可识别等）：只能确认公开下载，不能自动发现。
        return DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    if result.listing_page_reachable:
        # 页面可达但候选行不可识别（matching_candidate_count == 0），且无 PDF 证据。
        return DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    return DisclosureAccessMode.UNAVAILABLE


def is_auto_discoverable(access_mode: DisclosureAccessMode) -> bool:
    """接入形态是否支持自动化发现（不依赖隐藏接口/授权）。"""
    return access_mode in (
        DisclosureAccessMode.DOCUMENTED_API,
        DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML,
    )
