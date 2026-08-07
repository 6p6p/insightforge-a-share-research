"""CLI: 官方披露来源可行性探测（开发期诊断工具，非公告采集）。

调用方式：
    conda run -n insightforge python -m app.cli.probe_disclosure_sources \\
        --providers sse,cninfo \\
        --security-code 600519 \\
        --start-date 2026-01-01 \\
        --end-date 2026-08-07

约束：
- 只探测 Source Registry 已登记且 enabled 的 Provider（本阶段仅 sse、cninfo）；
- 只访问 Provider allowed_domains 内的 https URL（is_url_allowed）；
- 单个 Provider 最多 6 个请求，整次 CLI 最多 12 个；
- 不批量分页、不扫描路径、不猜 API endpoint、不调用内部数据服务接口、不执行 JS、不使用浏览器；
- 候选识别基于页面真实 <a href> 链接（security_code + 非空标题 + 日期文本
  + urljoin 后重新校验 allowlist）；
- 日期/公司筛选未送入合规查询入口时 search_request_applied=False，不宣称已筛选；
- 输出 JSON，不写数据库，不下载/保留响应正文。
"""

import argparse
import asyncio
import json
import re
import sys
from dataclasses import asdict, replace
from datetime import UTC, date, datetime
from urllib.parse import urljoin, urlparse

import structlog

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.disclosures.contracts import DisclosureProbeContext
from app.disclosures.decision import decide_access_mode, is_auto_discoverable
from app.disclosures.probe import (
    NOTE_API_ACCESS_TERMS_NOT_VERIFIED,
    NOTE_DOCUMENTED_API_REQUIRES_REGISTRATION,
    NOTE_NO_MATCHING_CANDIDATE_ROW,
    NOTE_OFFICIAL_DATA_SERVICE_LINK_FOUND,
    NOTE_OFFICIAL_PDF_LINK_VERIFIED,
    NOTE_PROBE_HTTP_ERROR,
    NOTE_SEARCH_REQUEST_NOT_APPLIED,
    DisclosureAccessMode,
    DisclosureProbeResult,
)
from app.disclosures.probe_client import (
    Link,
    ProbeClient,
    ProbeFetchError,
    ProbeLimitExceeded,
    ProbeRedirectLoop,
    ProbeResponseTooLarge,
    ProbeUrlNotAllowed,
    extract_links,
)
from app.repositories.source_provider_repository import SourceProviderRepository
from app.source_registry.url_policy import is_url_allowed

configure_asyncio_runtime()

_PROBE_ALLOWED_PROVIDERS = frozenset({"sse", "cninfo"})
_MAX_TOTAL_REQUESTS = 12
_PROVIDER_REQUEST_LIMIT = 6

# 公开官方入口：SSE 保留公告查询页；CNINFO 只从官方首页开始，不直接进 /new/disclosure。
_PROBE_ENTRY_URLS: dict[str, str] = {
    "sse": "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
    "cninfo": "https://www.cninfo.com.cn/",
}

# API 文档入口标记：必须出现在锚点文本或 href 中，才视为已确认的官方 API 文档入口。
_API_DOC_MARKERS = ("openapi", "api docs", "api 文档", "开发者文档", "swagger", "开放平台")
# 明确认证条款标记：只在 documented_api_found 时生效；
# "注册/登录/授权"等通用字样不构成认证信号。
_AUTH_TERM_MARKERS = ("api key", "apikey", "appid", "access token", "合同", "订阅", "申请权限")
# 目标候选的发布时间文本（YYYY[-/年]MM 形式，如 2026-04、2026年04）。
_DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}")
# 最多验证前几个匹配候选的 PDF 链接，控制单 Provider 请求数。
_MAX_PDF_CHECKS = 3


def _configure_probe_logging() -> None:
    """探测日志走 stderr，保证 stdout 只输出 JSON 报告。"""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def _resolve_allowed_url(
    href: str,
    base_url: str,
    allowed_domains: list[str],
) -> str | None:
    """urljoin 后重新执行 https + allowlist 校验；不合法返回 None。"""
    try:
        url = urljoin(base_url, href)
    except ValueError:
        return None
    if not is_url_allowed(url, allowed_domains):
        return None
    return url


def _is_matching_candidate_link(link: Link, security_code: str) -> bool:
    """目标候选 5 条件（前 4 项；allowlist 由调用方先完成 urljoin 校验）。

    - 非空标题；security_code 出现在链接上下文；发布时间文本存在；
    - 链接可解析（href 非空，LinkExtractor 已过滤 JS/锚点）。
    """
    if not link.text.strip():
        return False
    if security_code not in link.context:
        return False
    if not _DATE_RE.search(link.context):
        return False
    return True


def _matching_candidate_urls(
    links: list[Link],
    security_code: str,
    allowed_domains: list[str],
) -> list[str]:
    """返回满足 5 条件的候选链接（已 urljoin + 重新执行 allowlist 校验）。"""
    urls: list[str] = []
    for link in links:
        resolved = _resolve_allowed_url(link.href, link.base_url, allowed_domains)
        if resolved is None:
            continue
        if _is_matching_candidate_link(link, security_code):
            urls.append(resolved)
    return urls


def _is_pdf_content_type(content_type: str | None) -> bool:
    """PDF 验证基于 Content-Type（不只看 .pdf 后缀）。"""
    if not content_type:
        return False
    return content_type.split(";", 1)[0].strip().lower() == "application/pdf"


def probe_api_signals(body: bytes, base_url: str) -> tuple[bool, bool]:
    """识别已确认的官方 API 文档入口与明确认证条款。

    - api_found：API 文档标记必须出现在锚点文本或 href 中（代表入口）；
    - auth_required：只在 api_found 时判断，且只认明确凭据/合同/订阅类字样，
      不把"注册/登录/授权"当认证信号。
    """
    if not body:
        return False, False
    links = extract_links(body, base_url)
    link_text = " ".join(link.text for link in links).lower()
    link_hrefs = " ".join(link.href for link in links).lower()
    api_found = any(marker in link_text or marker in link_hrefs for marker in _API_DOC_MARKERS)
    if not api_found:
        return False, False
    page_text = body.decode("utf-8", errors="replace").lower()
    auth_required = any(marker in page_text for marker in _AUTH_TERM_MARKERS)
    return api_found, auth_required


async def _probe_provider(
    client: ProbeClient,
    context: DisclosureProbeContext,
) -> DisclosureProbeResult:
    checked_at = datetime.now(UTC)
    entry_url = _PROBE_ENTRY_URLS[client._provider_key]
    notes: list[str] = []

    try:
        page = await client.fetch_page(entry_url)
    except (ProbeFetchError, ProbeRedirectLoop) as exc:
        return _unavailable(
            client._provider_key,
            checked_at,
            client,
            (NOTE_PROBE_HTTP_ERROR, type(exc).__name__),
        )
    except ProbeResponseTooLarge:
        return _unavailable(client._provider_key, checked_at, client, (NOTE_PROBE_HTTP_ERROR,))
    except ProbeUrlNotAllowed:
        return _unavailable(client._provider_key, checked_at, client, ("probe_url_not_allowed",))
    except ProbeLimitExceeded:
        return _unavailable(client._provider_key, checked_at, client, ("probe_request_limit",))

    listing_reachable = page.status_code == 200
    listing_status_code = page.status_code
    final_hostname = urlparse(page.final_url).hostname or None
    response_type = page.response_type

    # 日期/公司筛选没有送入任何合规查询入口：只记录上下文，不宣称已筛选。
    search_request_applied = False
    notes.append(NOTE_SEARCH_REQUEST_NOT_APPLIED)

    matching_candidate_count = 0
    direct_pdf = False
    if listing_reachable:
        links = extract_links(page.body, page.final_url)
        allowed = client.allowed_domains
        matching_urls = _matching_candidate_urls(links, context.security_code, allowed)
        matching_candidate_count = len(matching_urls)
        if matching_candidate_count:
            for url in matching_urls[:_MAX_PDF_CHECKS]:
                try:
                    pdf = await client.fetch_pdf_head(url)
                    if pdf.status_code == 200 and _is_pdf_content_type(pdf.content_type):
                        direct_pdf = True
                        notes.append(NOTE_OFFICIAL_PDF_LINK_VERIFIED)
                        break
                except (ProbeFetchError, ProbeRedirectLoop, ProbeUrlNotAllowed):
                    continue
        else:
            notes.append(NOTE_NO_MATCHING_CANDIDATE_ROW)

        if client._provider_key == "cninfo":
            # 首页可能链接到 webapi.cninfo.com.cn 数据服务：只记录，不调用内部接口。
            webapi_found = any(
                "webapi.cninfo.com.cn" in url
                for link in links
                if (url := _resolve_allowed_url(link.href, link.base_url, allowed)) is not None
            )
            if webapi_found:
                notes.append(NOTE_OFFICIAL_DATA_SERVICE_LINK_FOUND)
                notes.append(NOTE_API_ACCESS_TERMS_NOT_VERIFIED)
    else:
        notes.append(NOTE_NO_MATCHING_CANDIDATE_ROW)

    documented_api, auth_required = probe_api_signals(
        page.body if listing_reachable else b"",
        page.final_url if listing_reachable else "",
    )
    if documented_api and auth_required:
        notes.append(NOTE_DOCUMENTED_API_REQUIRES_REGISTRATION)

    probe_result = DisclosureProbeResult(
        provider_key=client._provider_key,
        checked_at=checked_at,
        access_mode=DisclosureAccessMode.UNAVAILABLE,
        listing_page_reachable=listing_reachable,
        listing_results_visible_in_html=matching_candidate_count > 0,
        direct_pdf_verified=direct_pdf,
        documented_api_found=documented_api,
        authentication_required=auth_required,
        request_count=client.request_count,
        notes=(),
        listing_status_code=listing_status_code,
        final_hostname=final_hostname,
        response_type=response_type,
        search_request_applied=search_request_applied,
        matching_candidate_count=matching_candidate_count,
    )
    access_mode = decide_access_mode(probe_result)
    return replace(
        probe_result,
        access_mode=access_mode,
        notes=tuple(dict.fromkeys(notes)),
    )


def _unavailable(
    provider_key: str,
    checked_at: datetime,
    client: ProbeClient,
    notes: tuple[str, ...],
) -> DisclosureProbeResult:
    return DisclosureProbeResult(
        provider_key=provider_key,
        checked_at=checked_at,
        access_mode=DisclosureAccessMode.UNAVAILABLE,
        listing_page_reachable=False,
        listing_results_visible_in_html=False,
        direct_pdf_verified=False,
        documented_api_found=False,
        authentication_required=False,
        request_count=client.request_count,
        notes=notes,
        listing_status_code=None,
        final_hostname=None,
        response_type=None,
        search_request_applied=False,
        matching_candidate_count=0,
    )


def _disabled_result(provider_key: str, note: str) -> DisclosureProbeResult:
    return DisclosureProbeResult(
        provider_key=provider_key,
        checked_at=datetime.now(UTC),
        access_mode=DisclosureAccessMode.UNAVAILABLE,
        listing_page_reachable=False,
        listing_results_visible_in_html=False,
        direct_pdf_verified=False,
        documented_api_found=False,
        authentication_required=False,
        request_count=0,
        notes=(note,),
        listing_status_code=None,
        final_hostname=None,
        response_type=None,
        search_request_applied=False,
        matching_candidate_count=0,
    )


async def _probe_all(
    providers: list[str],
    context: DisclosureProbeContext,
) -> list[DisclosureProbeResult]:
    settings = get_settings()
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    results: list[DisclosureProbeResult] = []
    total_requests = 0
    try:
        async with database.session_factory()() as session:
            repo = SourceProviderRepository(session)
            for key in providers:
                if total_requests >= _MAX_TOTAL_REQUESTS:
                    break
                provider = await repo.get_by_key(key)
                if provider is None:
                    results.append(_disabled_result(key, "provider_not_found"))
                    continue
                if not provider.enabled:
                    results.append(_disabled_result(key, "provider_disabled"))
                    continue
                client = ProbeClient(
                    provider_key=key,
                    allowed_domains=list(provider.allowed_domains),
                    provider_request_limit=min(
                        _PROVIDER_REQUEST_LIMIT, _MAX_TOTAL_REQUESTS - total_requests
                    ),
                )
                result = await _probe_provider(client, context)
                results.append(result)
                total_requests += result.request_count
    finally:
        await database.dispose()
    return results


async def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="官方披露来源可行性探测（开发期诊断）")
    parser.add_argument("--providers", default="sse,cninfo", help="逗号分隔的 Provider key")
    parser.add_argument("--security-code", default="600519", help="六位证券代码")
    parser.add_argument("--start-date", type=_parse_date, default=date(2026, 1, 1))
    parser.add_argument("--end-date", type=_parse_date, default=date(2026, 8, 7))
    args = parser.parse_args(argv)

    providers = [p.strip() for p in args.providers.split(",") if p.strip()]
    for key in providers:
        if key not in _PROBE_ALLOWED_PROVIDERS:
            payload = {"error": f"provider not allowed for probing: {key}"}
            msg = json.dumps(payload, ensure_ascii=True)
            print(msg)
            return 2
    if not providers:
        print(json.dumps({"error": "no providers specified"}, ensure_ascii=True))
        return 2

    context = DisclosureProbeContext(
        security_code=args.security_code,
        start_date=args.start_date,
        end_date=args.end_date,
    )
    results = await _probe_all(providers, context)
    auto = [r for r in results if is_auto_discoverable(r.access_mode)]
    selected = auto[0].provider_key if auto else None

    if selected is None and results:
        modes = ", ".join(f"{r.provider_key}={r.access_mode.value}" for r in results)
        decision = (
            f"无合规自动发现通路：{modes}；不实现生产 Adapter，"
            "保留用户上传 + URL 导入 + 后续网络搜索发现兜底。"
        )
    else:
        decision = f"选择 {selected} 作为首个官方 Disclosure Provider。"

    report = {
        "request": {
            "security_code": context.security_code,
            "start_date": context.start_date.isoformat(),
            "end_date": context.end_date.isoformat(),
        },
        "providers": [asdict(r) for r in results],
        "total_requests": sum(r.request_count for r in results),
        "selected_candidate_provider": selected,
        "decision": decision,
    }
    # ensure_ascii=True：输出纯 ASCII 的 JSON，避免 Windows 控制台/conda run 的
    # GBK 重编码把中文转义成乱码；json.loads 后可还原为正确中文。
    print(json.dumps(report, ensure_ascii=True, indent=2, default=str))
    return 0


if __name__ == "__main__":
    _configure_probe_logging()
    raise SystemExit(asyncio.run(_main(sys.argv[1:])))
