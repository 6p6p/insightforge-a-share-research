"""IR discovery provider (P3: Company Website/IR Discovery).

issuer_domains registry（A 股公司登记官网域名）→ 公司官网有界爬取 →
issuer_ir_material SourceRecord 落库（provider=issuer_official、Tier-2、
critical_claim_eligible=True、acquisition_method=automatic_discovery）。

安全与诚实边界（不放松既有策略）：
- 只访问该公司登记的官网域名（IssuerDomainService.lookup_domains +
  validate_issuer_url 同规则；SafeHtmlFetcher / SafePdfFetcher 的 DNS 预检与
  allowlist 校验在底层保证）；
- **有界**：种子首页 ≤ 2、抓取 HTML 页 ≤ 8、PDF ≤ 3、仅 https、仅同 host
  （或子域）、单页 ≤ 5MiB（fetcher 内建）；
- content-type 验证：HTML 必须 text/html（SafeHtmlFetcher）；PDF 走
  SafePdfFetcher（%PDF 校验 + 大小上限）；
- URL canonicalization：urljoin 相对链接、去 fragment、query 上限、
  资源后缀排除、IR 栏目关键词优先；
- 幂等：同一 (company, provider_key, source_url) 不重复抓取/落库（既有
  uq_source_records_provider_url_artifact 兜底）；
- published_at 恒 None（官网 IR 页面无可靠发布时间——绝不伪造；no-lookahead
  由 acquired_at 承担，与 news 同语义）；reporting_period_end 恒 None；
- **0 事实编造**：只把真实抓取的官方页面落库；不生成 evidence / 数字 /
  事实；失败 → exhausted（SOURCE_NOT_FOUND → human fallback）。
"""

from __future__ import annotations

import re
import time
from collections import deque
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit, urlunsplit
from uuid import UUID

from lxml import html as lxml_html
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.acquisition.html_fetcher import SafeHtmlFetcher
from app.acquisition.http_fetcher import SafePdfFetcher
from app.core.logging import get_logger
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.issuer_domain_service import IssuerDomainService
from app.services.source_discovery.contracts import (
    REASON_DISCOVERY_FAILED,
    REASON_NO_CANDIDATES,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)
from app.source_registry.url_policy import _idna_host
from app.storage.raw_store import LocalRawArtifactStore

logger = get_logger("app.source_discovery.ir")

# ---------------------------------------------------------------- bounds

_MAX_SEED_DOMAINS = 2  # 最多从 2 个登记域名开始（有界）
_MAX_PAGES = 8  # 抓取的 HTML 页总数上限（含首页）
_MAX_PDFS = 3  # 抓取的 PDF 材料上限
_MAX_DEPTH = 2  # 链接深度上限（首页=0；越深噪音越大，严格有界）
_MAX_QUERY_LEN = 200  # URL query 上限（防无限参数页面）
_MAX_TITLE_LEN = 200

# 资源后缀：不作为页面抓取候选（HTML 之外的静态资源）。
_RESOURCE_PATH = re.compile(
    r"\\.(?:js|css|png|jpe?g|gif|svg|ico|webp|woff2?|ttf|otf|eot|map|json|xml|zip|rar|7z)$",
    re.IGNORECASE,
)

# 常见 A 股官网 IR 栏目路径关键词（URL 匹配，非内容判断——只用于候选排序，
# 命中优先抓取；未命中的站内页面仍受总量上界约束）。
_IR_PATH_KEYWORDS = (
    "investor",
    "tzzgx",
    "gongsiguanli",
    "relationship",
    "gudong",
    "stockholder",
    "shareholder",
    "touzi",
)

# 链接文本关键词（真实官网链接文本多样：宁德时代 IR 栏目路径
# /inverelations/ 无 URL 关键词——链接文本 "Investor Relations" 命中；
# 文本匹配只用于候选排序，不构成任何内容判断）。
_IR_TEXT_KEYWORDS = (
    "投资者关系",
    "股东",
    "业绩说明",
    "investor relations",
    "investor",
    "shareholder",
    "stockholder",
)

_IR_TEXT_RE = [re.compile(re.escape(kw), re.IGNORECASE) for kw in _IR_TEXT_KEYWORDS]


def _ir_score(key: str, text: str = "") -> int:
    lowered = key.lower()
    score = sum(1 for kw in _IR_PATH_KEYWORDS if kw in lowered)
    for pattern in _IR_TEXT_RE:
        if pattern.search(text):
            score += 1
    return score


class IrDiscoveryProvider:
    """公司官网 IR 材料发现（P3）：issuer_domains → 有界爬取 → 落库。"""

    provider_key = "issuer_ir_discovery"

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker | None = None,
        raw_store: LocalRawArtifactStore | None = None,
        html_fetcher: SafeHtmlFetcher | None = None,
        pdf_fetcher: SafePdfFetcher | None = None,
        issuer_domains: IssuerDomainService | None = None,
        enabled: bool = True,
        max_pages: int = _MAX_PAGES,
        max_pdfs: int = _MAX_PDFS,
        max_seed_domains: int = _MAX_SEED_DOMAINS,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._html_fetcher = html_fetcher
        self._pdf_fetcher = pdf_fetcher
        self._issuer_domains = issuer_domains
        self._enabled = enabled
        self._max_pages = max_pages
        self._max_pdfs = max_pdfs
        self._max_seed_domains = max_seed_domains

    def supports(self, request: SourceDiscoveryRequest) -> bool:
        return request.need_kind == "document" and request.source_type == "issuer_ir_material"

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        if (
            not self._enabled
            or self._sessionmaker is None
            or self._raw_store is None
            or request.as_of is None
            or not request.security_code
        ):
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        try:
            domains = await self._load_domains(request.company_id)
        except Exception:  # noqa: BLE001 - 发现失败 → exhausted（不泄漏异常）
            logger.warning(
                "ir_discovery_domain_lookup_failed",
                company_id=str(request.company_id),
                error_type="domain_lookup",
            )
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_DISCOVERY_FAILED,
                exhausted=True,
            )
        if not domains:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )

        html_fetcher = self._html_fetcher or SafeHtmlFetcher()
        pdf_fetcher = self._pdf_fetcher or SafePdfFetcher()
        try:
            already = await self._existing_urls(request.company_id)
        except Exception:  # noqa: BLE001
            already = set()
        seeded = set()
        for domain in domains[: self._max_seed_domains]:
            seeded.add(f"https://{domain}/")

        queue: deque[tuple[str, int]] = deque()  # (url, depth)
        visited: set[str] = set()
        for seed in sorted(seeded):
            queue.append((seed, 0))

        source_ids: list[UUID] = []
        pages_fetched = 0
        pdfs_fetched = 0
        started = time.monotonic()
        while queue and pages_fetched < self._max_pages:
            url, depth = queue.popleft()
            canonical = self._canonicalize(url, None)
            if canonical is None or not self._host_allowed(canonical["host"], domains):
                continue
            if canonical["key"] in visited or canonical["url"] in already:
                continue
            # 抓取时才标记 visited（入队不标记——同一 URL 入队多次无害）。
            visited.add(canonical["key"])
            try:
                page = await html_fetcher.fetch(canonical["url"], self.provider_key, domains)
            except Exception:  # noqa: BLE001 - 单页失败不阻塞
                logger.info(
                    "ir_discovery_page_fetch_failed",
                    host=canonical["host"],
                    error_type="fetch",
                )
                continue
            pages_fetched += 1
            source_id = await self._persist_html(request.company_id, page.final_url, page.raw_bytes)
            if source_id is not None:
                source_ids.append(source_id)
                already.add(page.final_url)
            # 扩展链接（有界：剩余页数 + PDF 上限；host 必须在登记域名内）。
            try:
                candidates = self._extract_candidates(page.raw_bytes, page.final_url)
            except Exception:  # noqa: BLE001
                candidates = []
            for cand in candidates:
                if pdfs_fetched >= self._max_pdfs:
                    break
                if not self._host_allowed(cand["host"], domains):
                    continue
                if cand["key"] in visited or cand["url"] in already:
                    continue
                if cand["pdf"]:
                    visited.add(cand["key"])
                    try:
                        fetched = await pdf_fetcher.fetch(
                            cand["url"], domains, self._max_pdf_bytes()
                        )
                    except Exception:  # noqa: BLE001 - 单 PDF 失败不阻塞
                        continue
                    pdfs_fetched += 1
                    try:
                        source_id = await self._persist_pdf(
                            request.company_id, fetched.final_url, fetched
                        )
                    finally:
                        fetched.close()
                    if source_id is not None:
                        source_ids.append(source_id)
                        already.add(fetched.final_url)
                else:
                    # 只扩展 IR 命中页（URL/链接文本关键词；首页恒抓）。语言站/
                    # 导航页等非 IR 页面不入队——避免噪音占据有界预算。深度
                    # 同时受 _MAX_DEPTH 约束。
                    if depth + 1 > _MAX_DEPTH or cand["score"] == 0:
                        continue
                    queue.append((cand["url"], depth + 1))
        logger.info(
            "ir_discovery_done",
            company_id=str(request.company_id),
            pages_fetched=pages_fetched,
            pdfs_fetched=pdfs_fetched,
            acquired=len(source_ids),
            duration_ms=int((time.monotonic() - started) * 1000),
        )
        if source_ids:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=True,
                source_ids=tuple(dict.fromkeys(source_ids)),
            )
        return SourceDiscoveryResult(
            provider_key=self.provider_key,
            acquired=False,
            reason=REASON_NO_CANDIDATES,
            exhausted=True,
        )

    # ------------------------------------------------------------ internals

    def _max_pdf_bytes(self) -> int:
        return 100 * 1024 * 1024

    @staticmethod
    def _host_allowed(host: str, domains: list[str]) -> bool:
        """host 必须在公司登记域名内（同域或子域；与 validate_issuer_url 同规则）。"""
        return any(host == d or host.endswith("." + d) for d in domains)

    async def _load_domains(self, company_id: UUID) -> list[str]:
        service = self._issuer_domains or IssuerDomainService(self._sessionmaker)
        return await service.lookup_domains(company_id)

    async def _existing_urls(self, company_id: UUID) -> set[str]:
        """该公司 issuer_official 已落库的 source_url（跨 run 幂等）。"""
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(SourceRecordModel.source_url).where(
                            SourceRecordModel.company_id == company_id,
                            SourceRecordModel.provider_key == "issuer_official",
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {url for url in rows if url is not None}

    def _extract_candidates(self, raw_bytes: bytes, base_url: str) -> list[dict]:
        """提取 <a href> + 链接文本 → 规范化 → 过滤 → IR 优先排序。"""
        doc = lxml_html.document_fromstring(raw_bytes)
        links = []
        for a in doc.xpath("//a[@href]"):
            href = a.get("href")
            if not href:
                continue
            text = " ".join((a.text_content() or "").split())
            links.append((href, text[:100]))
        seen: set[str] = set()
        result: list[dict] = []
        for href, text in links:
            canonical = self._canonicalize(href, base_url)
            if canonical is None or canonical["key"] in seen:
                continue
            seen.add(canonical["key"])
            result.append(
                {
                    "url": canonical["url"],
                    "key": canonical["key"],
                    "host": canonical["host"],
                    "pdf": canonical["pdf"],
                    "score": _ir_score(canonical["key"], text),
                }
            )
        result.sort(key=lambda c: c["score"], reverse=True)
        return result

    def _canonicalize(self, url: str, base: str | None) -> dict | None:
        """URL 规范化（有界爬取用）：https / 无 userinfo-port；host 校验在外层。

        返回 {url, host, key, pdf, score}；不合法 → None。
        """
        try:
            joined = urljoin(base or "", url) if base is not None else url
            parsed = urlsplit(joined)
            if parsed.scheme != "https":
                return None
            if (
                parsed.username is not None
                or parsed.password is not None
                or parsed.port is not None
            ):
                return None
            if not parsed.hostname:
                return None
            host = _idna_host(parsed.hostname)
            path = parsed.path or "/"
            if _RESOURCE_PATH.search(path):
                return None
            query = parsed.query
            if len(query) > _MAX_QUERY_LEN:
                query = ""
            clean = urlunsplit(("https", host, path, query, ""))
            key = f"{host}{path.rstrip('/') or '/'}" + (f"?{query}" if query else "")
            return {
                "url": clean,
                "host": host,
                "key": key,
                "pdf": path.lower().endswith(".pdf"),
                "score": _ir_score(key),
            }
        except Exception:  # noqa: BLE001 - 单个 URL 规范化失败不影响整体
            return None

    async def _persist_html(
        self, company_id: UUID, final_url: str, raw_bytes: bytes
    ) -> UUID | None:
        """HTML 页面落库：RawArtifact + SourceRecord（provider=issuer_official）。"""
        try:
            stored = self._raw_store.put_html_bytes(raw_bytes)
            title = self._extract_title(raw_bytes) or f"官网页面 {final_url}"
            async with self._sessionmaker() as session:
                artifact = await RawArtifactRepository(session).create(
                    RawArtifactModel(
                        content_sha256=stored.content_sha256,
                        storage_key=stored.storage_key,
                        byte_size=stored.byte_size,
                        media_type=stored.media_type,
                    )
                )
                if artifact is None:
                    artifact = await RawArtifactRepository(session).get_by_sha256(
                        stored.content_sha256
                    )
                    if artifact is None:
                        return None
                record = SourceRecordModel(
                    company_id=company_id,
                    provider_key="issuer_official",
                    artifact_id=artifact.artifact_id,
                    document_type="issuer_ir_material",
                    title=title[:_MAX_TITLE_LEN],
                    published_at=None,
                    reporting_period_end=None,
                    source_url=final_url,
                    acquisition_method="automatic_discovery",
                    status="available",
                    authority_tier_snapshot=2,
                    critical_claim_eligible_snapshot=True,
                    provider_capabilities_snapshot=["issuer_ir"],
                    acquired_at=datetime.now(UTC),
                )
                row = await SourceRecordRepository(session).create(record)
                await session.commit()
                return row.source_id if row is not None else None
        except Exception:  # noqa: BLE001 - 单页落库失败不阻塞
            logger.warning(
                "ir_discovery_persist_failed",
                company_id=str(company_id),
                error_type="html_persist",
            )
            return None

    async def _persist_pdf(
        self,
        company_id: UUID,
        final_url: str,
        fetched,
    ) -> UUID | None:
        try:
            stored = self._raw_store.put_pdf_stream(fetched.content_stream)
            title = f"官网材料 {final_url}"
            async with self._sessionmaker() as session:
                artifact = await RawArtifactRepository(session).create(
                    RawArtifactModel(
                        content_sha256=stored.content_sha256,
                        storage_key=stored.storage_key,
                        byte_size=stored.byte_size,
                        media_type=stored.media_type,
                    )
                )
                if artifact is None:
                    artifact = await RawArtifactRepository(session).get_by_sha256(
                        stored.content_sha256
                    )
                    if artifact is None:
                        return None
                record = SourceRecordModel(
                    company_id=company_id,
                    provider_key="issuer_official",
                    artifact_id=artifact.artifact_id,
                    document_type="issuer_ir_material",
                    title=title[:_MAX_TITLE_LEN],
                    published_at=None,
                    reporting_period_end=None,
                    source_url=final_url,
                    acquisition_method="automatic_discovery",
                    status="available",
                    authority_tier_snapshot=2,
                    critical_claim_eligible_snapshot=True,
                    provider_capabilities_snapshot=["issuer_ir"],
                    acquired_at=datetime.now(UTC),
                )
                row = await SourceRecordRepository(session).create(record)
                await session.commit()
                return row.source_id if row is not None else None
        except Exception:  # noqa: BLE001
            logger.warning(
                "ir_discovery_persist_failed",
                company_id=str(company_id),
                error_type="pdf_persist",
            )
            return None

    @staticmethod
    def _extract_title(raw_bytes: bytes) -> str | None:
        try:
            doc = lxml_html.document_fromstring(raw_bytes)
            title = doc.findtext(".//title") or None
            if title:
                return " ".join(title.split())
        except Exception:  # noqa: BLE001
            return None
        return None
