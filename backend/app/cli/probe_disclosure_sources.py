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
- 不批量分页、不扫描路径、不猜 API endpoint、不执行 JS、不使用浏览器；
- 输出 JSON，不写数据库，不下载/保留响应正文。
"""

import argparse
import asyncio
import json
import sys
from dataclasses import asdict
from datetime import UTC, date, datetime

import structlog

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.disclosures.decision import decide_access_mode, is_auto_discoverable
from app.disclosures.probe import (
    NOTE_DOCUMENTED_API_REQUIRES_REGISTRATION,
    NOTE_OFFICIAL_PDF_LINK_VERIFIED,
    NOTE_PROBE_HTTP_ERROR,
    NOTE_RESULT_ROWS_NOT_IN_INITIAL_HTML,
    DisclosureAccessMode,
    DisclosureProbeResult,
)
from app.disclosures.probe_client import (
    ProbeClient,
    ProbeFetchError,
    ProbeLimitExceeded,
    ProbeRedirectLoop,
    ProbeResponseTooLarge,
    ProbeUrlNotAllowed,
)
from app.repositories.source_provider_repository import SourceProviderRepository

configure_asyncio_runtime()

_PROBE_ALLOWED_PROVIDERS = frozenset({"sse", "cninfo"})
_MAX_TOTAL_REQUESTS = 12
_PROVIDER_REQUEST_LIMIT = 6

# 公开官方查询入口（人工验收阶段只探测公开页面，不猜内部接口）。
_PROBE_ENTRY_URLS: dict[str, str] = {
    "sse": "https://www.sse.com.cn/disclosure/listedinfo/announcement/",
    "cninfo": "https://www.cninfo.com.cn/new/disclosure",
}

_API_MARKERS = ("openapi", "api docs", "api 文档", "开发者文档", "swagger", "开放平台")
_AUTH_MARKERS = ("api key", "apikey", "appid", "access token", "注册", "登录", "授权")
_RESULTS_MARKERS = ("公告", "披露", "定期报告", "临时公告", "董事会", "股东大会")


def _configure_probe_logging() -> None:
    """探测日志走 stderr，保证 stdout 只输出 JSON 报告。"""
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )


def _parse_date(value: str) -> date:
    return date.fromisoformat(value)


def looks_like_results_html(text: str) -> bool:
    """初始 HTML 是否包含披露结果内容（宽松判断，供测试用）。"""
    return any(marker in text for marker in _RESULTS_MARKERS)


def first_pdf_link(body: bytes) -> str | None:
    """从初始 HTML 中提取第一个 https 官方 PDF 链接；找不到返回 None。"""
    text = body.decode("utf-8", errors="replace")
    for marker in ('href="', "href='", "src='", 'src="'):
        start = 0
        while True:
            idx = text.find(marker, start)
            if idx == -1:
                break
            content_start = idx + len(marker)
            closing = text.find(marker[-1], content_start) if marker[-1] in ('"', "'") else -1
            if closing == -1:
                start = content_start
                continue
            url = text[content_start:closing]
            start = content_start
            if url.lower().endswith(".pdf") and url.startswith("https://"):
                return url
    return None


def probe_api_signals(body: bytes) -> tuple[bool, bool]:
    """在公开页面正文中查找文档化 API 与认证线索；不猜测 endpoint。"""
    if not body:
        return False, False
    text = body.decode("utf-8", errors="replace").lower()
    api_found = any(marker in text for marker in _API_MARKERS)
    auth_required = any(marker in text for marker in _AUTH_MARKERS)
    return api_found, auth_required


async def _probe_provider(
    client: ProbeClient,
    security_code: str,
) -> DisclosureProbeResult:
    checked_at = datetime.now(UTC).replace(tzinfo=None)
    entry_url = _PROBE_ENTRY_URLS[client._provider_key]
    notes: list[str] = []

    try:
        page = await client.fetch_page(entry_url)
    except (ProbeFetchError, ProbeRedirectLoop) as exc:
        return DisclosureProbeResult(
            provider_key=client._provider_key,
            checked_at=checked_at,
            access_mode=DisclosureAccessMode.UNAVAILABLE,
            listing_page_reachable=False,
            listing_results_visible_in_html=False,
            direct_pdf_verified=False,
            documented_api_found=False,
            authentication_required=False,
            request_count=client.request_count,
            notes=(NOTE_PROBE_HTTP_ERROR, type(exc).__name__),
        )
    except ProbeResponseTooLarge:
        return _unavailable(client._provider_key, checked_at, client, (NOTE_PROBE_HTTP_ERROR,))
    except ProbeUrlNotAllowed:
        return _unavailable(client._provider_key, checked_at, client, ("probe_url_not_allowed",))
    except ProbeLimitExceeded:
        return _unavailable(client._provider_key, checked_at, client, ("probe_request_limit",))

    listing_reachable = page.status_code == 200
    results_visible = False
    direct_pdf = False
    if listing_reachable:
        text = page.body.decode("utf-8", errors="replace")
        results_visible = security_code in text or looks_like_results_html(text)
        if results_visible:
            pdf_url = first_pdf_link(page.body)
            if pdf_url is not None:
                try:
                    pdf = await client.fetch_pdf_head(pdf_url)
                    if pdf.status_code == 200:
                        direct_pdf = True
                        notes.append(NOTE_OFFICIAL_PDF_LINK_VERIFIED)
                except (ProbeFetchError, ProbeRedirectLoop, ProbeUrlNotAllowed):
                    pass
        else:
            notes.append(NOTE_RESULT_ROWS_NOT_IN_INITIAL_HTML)

    documented_api, auth_required = probe_api_signals(page.body if listing_reachable else b"")
    if documented_api and auth_required:
        notes.append(NOTE_DOCUMENTED_API_REQUIRES_REGISTRATION)

    access_mode = decide_access_mode(
        DisclosureProbeResult(
            provider_key=client._provider_key,
            checked_at=checked_at,
            access_mode=DisclosureAccessMode.UNAVAILABLE,
            listing_page_reachable=listing_reachable,
            listing_results_visible_in_html=results_visible,
            direct_pdf_verified=direct_pdf,
            documented_api_found=documented_api,
            authentication_required=auth_required,
            request_count=client.request_count,
            notes=(),
        )
    )
    return DisclosureProbeResult(
        provider_key=client._provider_key,
        checked_at=checked_at,
        access_mode=access_mode,
        listing_page_reachable=listing_reachable,
        listing_results_visible_in_html=results_visible,
        direct_pdf_verified=direct_pdf,
        documented_api_found=documented_api,
        authentication_required=auth_required,
        request_count=client.request_count,
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
    )


def _disabled_result(provider_key: str, note: str) -> DisclosureProbeResult:
    return DisclosureProbeResult(
        provider_key=provider_key,
        checked_at=datetime.now(UTC).replace(tzinfo=None),
        access_mode=DisclosureAccessMode.UNAVAILABLE,
        listing_page_reachable=False,
        listing_results_visible_in_html=False,
        direct_pdf_verified=False,
        documented_api_found=False,
        authentication_required=False,
        request_count=0,
        notes=(note,),
    )


async def _probe_all(providers: list[str], security_code: str) -> list[DisclosureProbeResult]:
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
                result = await _probe_provider(client, security_code)
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

    results = await _probe_all(providers, args.security_code)
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
