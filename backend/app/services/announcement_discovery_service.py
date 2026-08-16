"""Announcement discovery service (V1.1 final closure): East Money 公告自动发现。

CNINFO WAF 不可用（hisAnnouncement/query 恒返回空）时，年度/半年度/季度报告
的受控自动获取走 East Money 公告 API（Tier-3 后备，同 BSE 记录降级先例）：

- `discover(security_code, source_type, period, as_of)`：list API
  （np-anotice-stock.eastmoney.com）→ 按标题关键词（年度报告 / 半年度报告 /
  季度报告）+ 年份 + notice_date ≤ as_of（no-lookahead）确定性过滤，返回
  候选（art_code / title / notice_date）。**无 LLM、无模糊匹配**。
- `resolve_attach_url(art_code)`：content API（np-cnotice-stock.eastmoney.com）
  → attach_url（pdf.dfcfw.com）。
- `_fetch_pdf_bytes(attach_url, allowed_domains, max_bytes)`：先试 SafePdfFetcher；
  pdf.dfcfw.com 反爬 JS challenge（`__tst_status` / `EO_Bot_Ssid`，常量内嵌于
  challenge 脚本）时做**确定性 cookie 握手**后重取——仍然只访问 allowlist 域名，
  最终校验 `%PDF` 魔数与体积（**不绕过任何安全边界**）。
- `acquire_report(...)`：discover → resolve → download → 经
  SourceIngestionService 落库（provider=eastmoney、acquisition_method=
  automatic_discovery、external_document_id=art_code）→ 幂等（sha256 /
  (provider_key, source_url, artifact_id) replay）。

失败分类：网络/解析 → `AnnouncementDiscoveryError`（稳定 error_code）；无可下载
候选 → 返回 None（调用方保持原 SOURCE_NOT_FOUND 语义，落入 human fallback）。

**只作为 fulfillment 的受控增强**：discovery 失败绝不冒充来源（critical_claim
_eligible=False、Tier-3 快照由 eastmoney provider 行提供）。
"""

import re
from dataclasses import dataclass
from datetime import date

import httpx

from app.acquisition.http_fetcher import SafePdfFetcher
from app.db.models.source_provider import SourceProviderModel
from app.source_registry.url_policy import is_url_allowed

_ANNOUNCEMENT_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_REFERER = "https://data.eastmoney.com/"

# 每页条数（上限 50）。
_PAGE_SIZE = 50
# 扫描上限（1000 条公告 ≈ 大盘股 2 年公告量；防止无限分页）。
_MAX_PAGES = 20
# 无 period 过滤时的扫描下限（as_of - 400 天）。
_SCAN_WINDOW_DAYS = 400
# 扫描下限的绝对守卫（防误配 period 导致无限扫描）。
_SCAN_EARLIEST_GUARD = date(2000, 1, 1)

# 标题关键词（source_type → 必须包含；exclude 规则单独处理）。
_ANNUAL_KEYWORD = "年度报告"
_SEMIANNUAL_KEYWORD = "半年度报告"
_QUARTERLY_KEYWORD = "季度报告"
# issuer_ir_material 关键词（投资者关系材料 / 业绩说明会）。
_IR_KEYWORDS = ("投资者关系", "业绩说明会")

# company_announcement need 最多自动获取的最近公告数（有界：不无限下载）。
_MAX_ACQUIRE_ANNOUNCEMENTS = 2

# company_announcement 排除标题：法律意见/验资/评估等程序性文件通常是扫描件，
# 解析器无法提取文本（parse_failed 实测），且对业务研究价值低。
_ANNOUNCEMENT_EXCLUDE_TERMS = (
    "法律意见",
    "律师见证",
    "验资报告",
    "资产评估报告",
    "专项审计",
    "更正公告",
)


def _matches_announcement_title(title: str) -> bool:
    """公告标题匹配：排除程序性/扫描件类公告（法律意见、验资等）。"""
    if "摘要" in title or "英文" in title:
        return False
    return not any(term in title for term in _ANNOUNCEMENT_EXCLUDE_TERMS)


# pdf.dfcfw.com 反爬 challenge 常量提取（Alibaba __tst_status 型）：
#   t = WTKkN + bOYDu + wyeCN；EO_Bot_Ssid = (t, <ssid>) 中的常量。
_CHALLENGE_SUM_RE = re.compile(r"WTKkN:(\d+),.*?bOYDu:(\d+),.*?wyeCN:(\d+)")
_CHALLENGE_SSID_RE = re.compile(r"\(t,(\d+)\)")

# 真实 PDF 的最小体积（反爬页 / 错误页远小于此；正常年报 > 1MB）。
_MIN_PDF_BYTES = 1024


def parse_challenge_cookies(js_text: str) -> tuple[int, int] | None:
    """确定性解析 pdf.dfcfw.com 反爬 challenge → (__tst_status 值, EO_Bot_Ssid)。

    常量内嵌于 challenge 脚本（每次下发可能轮换）；解析失败 → None（调用方
    保持失败语义，不猜 cookie）。
    """
    sum_match = _CHALLENGE_SUM_RE.search(js_text)
    ssid_match = _CHALLENGE_SSID_RE.search(js_text)
    if sum_match is None or ssid_match is None:
        return None
    t = int(sum_match.group(1)) + int(sum_match.group(2)) + int(sum_match.group(3))
    return t, int(ssid_match.group(1))


def looks_like_pdf(data: bytes) -> bool:
    """确定性校验：真实 PDF 魔数 + 最小体积（拒绝反爬页 / 错误页）。"""
    return len(data) >= _MIN_PDF_BYTES and data[:4] == b"%PDF"


# 季度报告标题 → 报告期结束日（确定性解析；Q1/Q2/Q3/Q4）。
_QUARTER_MAP = {
    "一": 3,
    "二": 6,
    "三": 9,
    "四": 12,
}


def _scan_cutoff(source_type: str, period: str | None, as_of: date) -> date:
    """扫描下限（确定性）：period 存在时按报告发布窗口推导，否则 as_of - 400 天。

    年报 Y 最早在 Y+1-01-01 披露（常见 3-4 月）；半年报 Y 在 Y-07-01 起；
    季报 Y 在 Y-01-01 起。下限 = max(发布窗口起点, 绝对守卫)，保证旧年度
    报告（如 as_of=2025-12-31 时的 2023 年报，2024-03 披露）可被发现。
    """
    if period and re.fullmatch(r"\d{4}", period):
        year = int(period)
        if source_type == "annual_report":
            earliest = date(year + 1, 1, 1)
        elif source_type == "semiannual_report":
            earliest = date(year, 7, 1)
        elif source_type == "quarterly_report":
            earliest = date(year, 1, 1)
        else:
            earliest = date.fromordinal(as_of.toordinal() - _SCAN_WINDOW_DAYS)
        return max(earliest, _SCAN_EARLIEST_GUARD)
    return date.fromordinal(as_of.toordinal() - _SCAN_WINDOW_DAYS)


def reporting_period_end_for(source_type: str, period: str | None, title: str) -> date | None:
    """由 source_type + period + 标题确定性推导报告期结束日。

    - annual_report + period=2024 → 2024-12-31；
    - semiannual_report + period=2025 → 2025-06-30；
    - quarterly_report + period=2025 → 按标题「第X季度」→ 03-31/06-30/09-30/
      12-31（无法解析 → 09-30）。
    解析失败 → None（来源仍可落库，只是不满足按 period 过滤的 need）。
    """
    if not period or not re.fullmatch(r"\d{4}", period):
        return None
    year = int(period)
    if source_type == "annual_report":
        return date(year, 12, 31)
    if source_type == "semiannual_report":
        return date(year, 6, 30)
    if source_type == "quarterly_report":
        month = None
        for chinese, end_month in _QUARTER_MAP.items():
            # 兼容 "第X季度" 与 "X季度"（无"第"字，如 "2026年一季度报告"）。
            if f"第{chinese}季度" in title or f"{chinese}季度" in title:
                month = end_month
                break
        return date(year, month or 9, (30 if (month or 9) != 3 else 31))
    return None


class AnnouncementDiscoveryError(RuntimeError):
    """稳定错误码（网络/解析失败；不泄漏响应正文）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class DiscoveredAnnouncement:
    art_code: str
    title: str
    notice_date: date | None


@dataclass(frozen=True)
class AcquireResult:
    source_id: str
    title: str
    replayed: bool


class AnnouncementDiscoveryService:
    """受控公告发现（East Money Tier-3 后备；无 LLM、确定性过滤）。"""

    def __init__(
        self,
        sessionmaker=None,
        raw_store=None,
        client: httpx.AsyncClient | None = None,
        fetcher: SafePdfFetcher | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._client = client
        self._fetcher = fetcher or SafePdfFetcher()

    # ------------------------------------------------------------ discover

    async def discover(
        self,
        *,
        security_code: str,
        source_type: str,
        period: str | None,
        as_of: date,
    ) -> list[DiscoveredAnnouncement]:
        """确定性发现：标题关键词 + 年份 + no-lookahead（notice_date ≤ as_of）。

        source_type ∈ annual_report / semiannual_report / quarterly_report /
        company_announcement / issuer_ir_material / other。company_announcement
        / other 不做关键词过滤（返回窗口内全部公告）；issuer_ir_material 用
        投资者关系关键词（「投资者关系」「业绩说明会」）。
        """
        keyword = _keyword_for(source_type)
        cutoff = _scan_cutoff(source_type, period, as_of)
        results: list[DiscoveredAnnouncement] = []
        for page in range(1, _MAX_PAGES + 1):
            items = await self._fetch_page(security_code, page)
            if not items:
                break
            for item in items:
                notice = _parse_notice_date(item.get("notice_date"))
                if notice is not None and notice > as_of:
                    continue
                if notice is not None and notice < cutoff:
                    return results
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                if source_type == "issuer_ir_material":
                    if not _matches_ir_title(title):
                        continue
                elif source_type == "company_announcement":
                    if not _matches_announcement_title(title):
                        continue
                elif keyword is not None and not _matches_keyword(title, keyword):
                    continue
                if period and str(period) not in title:
                    continue
                art_code = str(item.get("art_code") or "").strip()
                if not art_code:
                    continue
                results.append(
                    DiscoveredAnnouncement(art_code=art_code, title=title, notice_date=notice)
                )
            if len(items) < _PAGE_SIZE:
                break
        return results

    # ------------------------------------------------------------ acquire

    async def acquire_report(
        self,
        *,
        company_id: str,
        security_code: str,
        source_type: str,
        period: str | None,
        as_of: date,
    ) -> AcquireResult | None:
        """discover → resolve → download → ingest → schedule prepare（幂等）。

        - 支持 annual/semiannual/quarterly（关键词过滤）+ company_announcement
          （最近公告，有界 ≤2）+ issuer_ir_material（投资者关系关键词）；
          news_article 不支持（需原创发布者验证链，留给用户路径）；
        - 候选逐个尝试，第一个成功落库即返回；company_announcement 尝试最近
          若干条（有界）；全部失败 → None（调用方保持 SOURCE_NOT_FOUND /
          human fallback，绝不冒充来源）；
        - 需要 `sessionmaker` / `raw_store`（构造时注入）；缺失 → 直接返回 None
          （只读发现场景不落库）。
        """
        if self._sessionmaker is None or self._raw_store is None:
            return None
        if source_type not in (
            "annual_report",
            "semiannual_report",
            "quarterly_report",
            "company_announcement",
            "issuer_ir_material",
        ):
            return None
        candidates = await self.discover(
            security_code=security_code,
            source_type=source_type,
            period=period,
            as_of=as_of,
        )
        if not candidates:
            return None
        if source_type == "company_announcement":
            candidates = candidates[:_MAX_ACQUIRE_ANNOUNCEMENTS]
        provider = await self._load_provider("eastmoney")
        if provider is None:
            return None
        from datetime import datetime
        from datetime import time as dtime

        from app.core.config import get_settings

        max_bytes = get_settings().source_max_file_size_bytes
        from app.services.source_ingestion_service import SourceIngestionService

        ingestion = SourceIngestionService(
            self._sessionmaker, self._raw_store, fetcher=self._fetcher, max_bytes=max_bytes
        )

        for candidate in candidates:
            try:
                attach_url = await self.resolve_attach_url(candidate.art_code)
                pdf_bytes = await self.fetch_pdf_bytes(
                    attach_url, provider.allowed_domains, max_bytes
                )
            except Exception:  # noqa: BLE001 - 单个候选失败 → 尝试下一个
                continue
            try:
                from app.domain.source_records import SourceDocumentType

                result = await ingestion.ingest_discovered_bytes(
                    company_id=company_id,
                    provider_key="eastmoney",
                    document_type=SourceDocumentType(source_type),
                    title=candidate.title,
                    source_url=attach_url,
                    published_at=(
                        datetime.combine(candidate.notice_date, dtime.min)
                        if candidate.notice_date is not None
                        else None
                    ),
                    reporting_period_end=reporting_period_end_for(
                        source_type, period, candidate.title
                    ),
                    external_document_id=candidate.art_code,
                    pdf_bytes=pdf_bytes,
                )
            except Exception:  # noqa: BLE001 - 落库失败 → 尝试下一个候选
                continue
            if result.replayed:
                continue  # 已存在相同来源 → 尝试下一个候选
            # 不在此处后台 schedule_prepare：fulfill 随后会对该来源同步
            # ensure_indexed（parse → chunk → index），并发后台任务会与同步
            # 路径竞争 Chroma/BGE（生产实测 index_failed / 连接池耗尽）。
            return AcquireResult(
                source_id=str(result.record.source_id),
                title=candidate.title,
                replayed=False,
            )
        return None

    # ------------------------------------------------------------ download

    async def resolve_attach_url(self, art_code: str) -> str:
        """content API → attach_url（pdf.dfcfw.com；失败 → AnnouncementDiscoveryError）。"""
        try:
            response = await self._get(
                _CONTENT_URL,
                {
                    "art_code": art_code,
                    "client_source": "web",
                    "page_index": 1,
                },
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AnnouncementDiscoveryError(
                code="announcement_content_fetch_failed",
                message="公告详情获取失败",
            ) from exc
        data = payload.get("data") or {}
        attach_url = str(data.get("attach_url") or "").strip()
        if not attach_url:
            raise AnnouncementDiscoveryError(
                code="announcement_attach_missing",
                message="公告无 PDF 附件",
            )
        return attach_url

    async def fetch_pdf_bytes(
        self,
        attach_url: str,
        allowed_domains: list[str],
        max_bytes: int,
    ) -> bytes:
        """下载并校验 PDF 字节（allowlist / SSRF 边界不变，无任何放宽）。

        1. 先走 SafePdfFetcher（标准路径，含重定向策略）；
        2. pdf.dfcfw.com 返回反爬 JS challenge 时，做确定性 cookie 握手
           （`parse_challenge_cookies` 解析内嵌常量 → `__tst_status` /
           `EO_Bot_Ssid`）后重取——握手请求同样只允许 allowlist 域名；
        3. 最终 `looks_like_pdf` 校验（%PDF 魔数 + 最小体积）——反爬页 /
           错误页绝不当作来源落库。
        """
        if not is_url_allowed(attach_url, allowed_domains):
            raise AnnouncementDiscoveryError(
                code="announcement_pdf_domain_not_allowed",
                message="PDF 地址不在来源机构受控域名内",
            )
        try:
            pdf = await self._fetcher.fetch(attach_url, allowed_domains, max_bytes)
            try:
                data = pdf.content_stream.read(max_bytes + 1)
            finally:
                pdf.close()
            if looks_like_pdf(data) and len(data) <= max_bytes:
                return data
        except Exception:  # noqa: BLE001 - 标准路径失败 → 尝试反爬握手
            pass
        data = await self._fetch_with_challenge_handshake(attach_url, allowed_domains, max_bytes)
        if not looks_like_pdf(data) or len(data) > max_bytes:
            raise AnnouncementDiscoveryError(
                code="announcement_pdf_fetch_failed",
                message="PDF 下载失败（反爬或内容异常）",
            )
        return data

    async def _fetch_with_challenge_handshake(
        self,
        attach_url: str,
        allowed_domains: list[str],
        max_bytes: int,
    ) -> bytes:
        """pdf.dfcfw.com 反爬 cookie 握手（确定性；仍只访问 allowlist 域名）。"""
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(connect=10.0, read=60.0, write=30.0, pool=10.0),
                follow_redirects=True,
            ) as client:
                first = await client.get(attach_url)
                if not is_url_allowed(str(first.url), allowed_domains):
                    raise AnnouncementDiscoveryError(
                        code="announcement_pdf_redirect_not_allowed",
                        message="PDF 重定向到非受控域名",
                    )
                if looks_like_pdf(first.content) and len(first.content) <= max_bytes:
                    return first.content
                challenge = first.text
                cookies = parse_challenge_cookies(challenge)
                if cookies is None:
                    raise AnnouncementDiscoveryError(
                        code="announcement_challenge_unparseable",
                        message="反爬 challenge 无法解析",
                    )
                t, ssid = cookies
                host = first.url.host or ""
                client.cookies.set("__tst_status", f"{t}#", domain=host, path="/")
                client.cookies.set("EO_Bot_Ssid", str(ssid), domain=host, path="/")
                second = await client.get(attach_url)
                if not is_url_allowed(str(second.url), allowed_domains):
                    raise AnnouncementDiscoveryError(
                        code="announcement_pdf_redirect_not_allowed",
                        message="PDF 重定向到非受控域名",
                    )
                return second.content
        except AnnouncementDiscoveryError:
            raise
        except (httpx.HTTPError, ValueError) as exc:
            raise AnnouncementDiscoveryError(
                code="announcement_pdf_fetch_failed",
                message="PDF 下载失败",
            ) from exc

    # ------------------------------------------------------------ internal

    async def _fetch_page(self, security_code: str, page: int) -> list[dict]:
        try:
            response = await self._get(
                _ANNOUNCEMENT_LIST_URL,
                {
                    "sr": -1,
                    "page_size": _PAGE_SIZE,
                    "page_index": page,
                    "ann_type": "A",
                    "client_source": "web",
                    "stock_list": security_code,
                    "f_node": 0,
                    "s_node": 0,
                },
            )
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise AnnouncementDiscoveryError(
                code="announcement_list_fetch_failed",
                message="公告列表获取失败",
            ) from exc
        data = payload.get("data") or {}
        items = data.get("list") or []
        return [item for item in items if isinstance(item, dict)]

    async def _get(self, url: str, params: dict) -> httpx.Response:
        """GET（注入 client 时复用且不关闭；否则用一次性 client）。"""
        if self._client is not None:
            return await self._client.get(url, params=params, headers={"Referer": _REFERER})
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=10.0, read=30.0, write=30.0, pool=10.0)
        ) as client:
            return await client.get(url, params=params, headers={"Referer": _REFERER})

    async def _load_provider(self, provider_key: str) -> SourceProviderModel | None:
        from sqlalchemy import select as sa_select

        from app.db.models.source_provider import SourceProviderModel as ProviderModel

        async with self._sessionmaker() as session:
            result = await session.execute(
                sa_select(ProviderModel).where(ProviderModel.provider_key == provider_key)
            )
            return result.scalar_one_or_none()

    async def _schedule_prepare(self, source_id: str) -> None:
        """后台准备（parse → chunk → index）；失败不阻止 acquisition 成功。"""
        try:
            from app.core.config import get_settings
            from app.rag.embedding.bge import BGEProvider
            from app.rag.index.service import VectorIndexService
            from app.services.chunking_service import ChunkingService
            from app.services.source_preparation_service import SourcePreparationService
            from app.vectorstore.client import ChromaManager

            settings = get_settings()
            chroma = ChromaManager(
                host=settings.chroma_host,
                port=settings.chroma_port,
                ssl=settings.chroma_ssl,
                timeout_seconds=settings.chroma_timeout_seconds,
            )
            preparation = SourcePreparationService(
                self._sessionmaker,
                self._raw_store,
                ChunkingService(self._sessionmaker),
                VectorIndexService(
                    sessionmaker=self._sessionmaker,
                    embedding_provider=BGEProvider(),
                    chroma=chroma,
                ),
            )
            preparation.schedule_prepare(source_id)
        except Exception:  # noqa: BLE001 - 预准备失败不阻止 acquisition 成功
            return


def _keyword_for(source_type: str) -> str | None:
    """source_type → 标题关键词（None = 不过滤）。"""
    if source_type == "annual_report":
        return _ANNUAL_KEYWORD
    if source_type == "semiannual_report":
        return _SEMIANNUAL_KEYWORD
    if source_type == "quarterly_report":
        return _QUARTERLY_KEYWORD
    return None


def _matches_keyword(title: str, keyword: str) -> bool:
    """关键词匹配：年度报告排除「半年度报告」；排除摘要/英文版标题。"""
    if keyword == _ANNUAL_KEYWORD and _SEMIANNUAL_KEYWORD in title:
        return False
    if keyword in title and "摘要" not in title and "英文" not in title:
        return True
    return False


def _matches_ir_title(title: str) -> bool:
    """投资者关系材料标题匹配（排除摘要/英文版）。"""
    if "摘要" in title or "英文" in title:
        return False
    return any(keyword in title for keyword in _IR_KEYWORDS)


def _parse_notice_date(raw: object) -> date | None:
    """'2026-08-12 00:00:00' / '2026-08-12' → date；非法 → None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
