"""Decision logic: turn probe results into an access-mode verdict.

决策规则（与阶段文档一致）：
1. 有正式公开文档 API → documented_api；
2. 搜索结果存在于首次返回的公开 HTML 且含可验证 PDF 链接 → public_server_rendered_html；
3. 只能确认公开 PDF 下载、但无法通过合规页面发现 → public_direct_pdf_only；
4. 需要注册/授权/合同/API Key → requires_auth_or_contract；
5. 页面只有搜索外壳、结果需 JS 或未公开接口 → requires_javascript_or_internal_endpoint；
6. 不得因为发现浏览器内部 JSON 请求就标记 documented_api；
7. 全部不可用 → unavailable。
"""

from app.disclosures.probe import DisclosureAccessMode, DisclosureProbeResult


def decide_access_mode(result: DisclosureProbeResult) -> DisclosureAccessMode:
    """根据单个 Provider 的探测结果给出接入形态结论。"""
    if result.documented_api_found:
        if result.authentication_required:
            return DisclosureAccessMode.REQUIRES_AUTH_OR_CONTRACT
        return DisclosureAccessMode.DOCUMENTED_API
    if result.listing_page_reachable and result.listing_results_visible_in_html:
        if result.direct_pdf_verified:
            return DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML
        # 页面有结果行但没有可验证的官方 PDF 链接：不能自动发现
        return DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
    if result.listing_page_reachable and not result.listing_results_visible_in_html:
        if result.direct_pdf_verified:
            return DisclosureAccessMode.PUBLIC_DIRECT_PDF_ONLY
        return DisclosureAccessMode.REQUIRES_JAVASCRIPT_OR_INTERNAL_ENDPOINT
    if result.authentication_required:
        return DisclosureAccessMode.REQUIRES_AUTH_OR_CONTRACT
    return DisclosureAccessMode.UNAVAILABLE


def is_auto_discoverable(access_mode: DisclosureAccessMode) -> bool:
    """接入形态是否支持自动化发现（不依赖隐藏接口/授权）。"""
    return access_mode in (
        DisclosureAccessMode.DOCUMENTED_API,
        DisclosureAccessMode.PUBLIC_SERVER_RENDERED_HTML,
    )
