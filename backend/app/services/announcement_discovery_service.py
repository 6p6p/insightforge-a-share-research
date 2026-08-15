"""Announcement discovery service (V1.1 final closure): East Money 公告自动发现。

CNINFO WAF 不可用（hisAnnouncement/query 恒返回空）时，年度/半年度/季度报告
的受控自动获取走 East Money 公告 API（Tier-3 后备，同 BSE 记录降级先例）：

- `discover(security_code, source_type, period, as_of)`：list API
  （np-anotice-stock.eastmoney.com）→ 按标题关键词（年度报告 / 半年度报告 /
  季度报告）+ 年份 + notice_date ≤ as_of（no-lookahead）确定性过滤，返回
  候选（art_code / title / notice_date）。**无 LLM、无模糊匹配**。
- `resolve_attach_url(art_code)`：content API（np-cnotice-stock.eastmoney.com）
  → attach_url（pdf.dfcfw.com）。
- `download_pdf(attach_url, allowed_domains, max_bytes)`：SafePdfFetcher
  （沿用现有 allowlist / SSRF 策略，无任何放宽）。
- `acquire_report(...)`：discover → resolve → download → 经
  SourceIngestionService 落库（provider=eastmoney、acquisition_method=
  automatic_discovery、external_document_id=art_code）→ 幂等（sha256 /
  (provider_key, source_url, artifact_id) replay）。

失败分类：网络/解析 → `AnnouncementDiscoveryError`（稳定 error_code）；无可下载
候选 → 返回 None（调用方保持原 SOURCE_NOT_FOUND 语义，落入 human fallback）。

**只作为 fulfillment 的受控增强**：discovery 失败绝不冒充来源（critical_claim
_eligible=False、Tier-3 快照由 eastmoney provider 行提供）。
"""

from dataclasses import dataclass
from datetime import date
from typing import BinaryIO

import httpx

from app.acquisition.http_fetcher import FetchedPdf, SafePdfFetcher
from app.db.models.source_provider import SourceProviderModel

_ANNOUNCEMENT_LIST_URL = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_CONTENT_URL = "https://np-cnotice-stock.eastmoney.com/api/content/ann"
_REFERER = "https://data.eastmoney.com/"

# 每页条数（上限 50）。
_PAGE_SIZE = 50
# 扫描上限（1000 条公告 ≈ 大盘股 2 年公告量；防止无限分页）。
_MAX_PAGES = 20
# 报告发布窗口：目标年报告最迟在次年 4 月底披露；扫描下限 = as_of - 400 天。
_SCAN_WINDOW_DAYS = 400

# 标题关键词（source_type → 必须包含；exclude 规则单独处理）。
_ANNUAL_KEYWORD = "年度报告"
_SEMIANNUAL_KEYWORD = "半年度报告"
_QUARTERLY_KEYWORD = "季度报告"


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
        company_announcement / other。company_announcement / other 不做关键词
        过滤（返回窗口内全部公告，调用方自行裁剪）。
        """
        keyword = _keyword_for(source_type)
        results: list[DiscoveredAnnouncement] = []
        for page in range(1, _MAX_PAGES + 1):
            items = await self._fetch_page(security_code, page)
            if not items:
                break
            for item in items:
                notice = _parse_notice_date(item.get("notice_date"))
                if notice is not None and notice > as_of:
                    continue
                if notice is not None and notice < date.fromordinal(
                    as_of.toordinal() - _SCAN_WINDOW_DAYS
                ):
                    return results
                title = str(item.get("title") or "").strip()
                if not title:
                    continue
                if keyword is not None and not _matches_keyword(title, keyword):
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

        - 只处理年度/半年度/季度报告（公告类过宽泛，留给用户上传）；
        - 候选逐个尝试，第一个成功落库即返回；全部失败 → None（调用方保持
          SOURCE_NOT_FOUND / human fallback，绝不冒充来源）；
        - 需要 `sessionmaker` / `raw_store`（构造时注入）；缺失 → 直接返回 None
          （只读发现场景不落库）。
        """
        if self._sessionmaker is None or self._raw_store is None:
            return None
        if source_type not in (
            "annual_report",
            "semiannual_report",
            "quarterly_report",
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
        provider = await self._load_provider("eastmoney")
        if provider is None:
            return None
        from app.core.config import get_settings

        max_bytes = get_settings().source_max_file_size_bytes
        from app.services.source_ingestion_service import SourceIngestionService

        ingestion = SourceIngestionService(
            self._sessionmaker, self._raw_store, fetcher=self._fetcher, max_bytes=max_bytes
        )
        from datetime import datetime, time as dtime

        for candidate in candidates:
            try:
                attach_url = await self.resolve_attach_url(candidate.art_code)
                pdf = await self.download_pdf(attach_url, provider.allowed_domains, max_bytes)
            except Exception:  # noqa: BLE001 - 单个候选失败 → 尝试下一个
                continue
            try:
                from app.domain.source_records import SourceDocumentType

                result = await ingestion.ingest_discovered(
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
                    reporting_period_end=None,
                    external_document_id=candidate.art_code,
                )
            except Exception:  # noqa: BLE001 - 落库失败 → 尝试下一个候选
                continue
            finally:
                pdf.close()
            if result.replayed:
                continue  # 已存在相同来源 → 尝试下一个候选
            await self._schedule_prepare(result.record.source_id)
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

    async def download_pdf(
        self,
        attach_url: str,
        allowed_domains: list[str],
        max_bytes: int,
    ) -> FetchedPdf:
        """SafePdfFetcher 下载（沿用 allowlist / SSRF 策略，不放宽）。"""
        return await self._fetcher.fetch(attach_url, allowed_domains, max_bytes)

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
            timeout=httpx.Timeout(connect=10.0, read=30.0)
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


def _parse_notice_date(raw: object) -> date | None:
    """'2026-08-12 00:00:00' / '2026-08-12' → date；非法 → None。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None
