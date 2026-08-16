"""Search discovery provider (P1 扩展点 / P2 Model Assisted Discovery)。

P2：注入 'SearchQueryModel'（deepseek-v4-flash）后启用完整链路：

    LLM 候选（URL + title）
        ↓ 候选校验（https + hostname，Pydantic 已强制）
        ↓ **域名 allowlist**：hostname ∈ registry enabled provider 的
          allowed_domains（精确/子域）或本公司 issuer_domains 域名
          （issuer_official）——不匹配 → 拒绝（绝不接受任意域名）
        ↓ SafeFetcher 真实抓取验证（provider allowlist 边界内）
        ↓ SourceIngestionService.ingest_discovered 落库（幂等）

**禁止**：生成 evidence / 财务数字 / 事实；绕过 provenance（候选不是
SourceRecord；抓取验证 + 域名校验后才落库）。LLM 未配置（P1 占位）→
exhausted + REASON_SEARCH_NOT_CONFIGURED。
"""

from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.acquisition.http_fetcher import SafePdfFetcher
from app.db.models.issuer_domain import IssuerDomainModel
from app.domain.source_records import SourceDocumentType
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.source_discovery.contracts import (
    REASON_NO_CANDIDATES,
    REASON_SEARCH_NOT_CONFIGURED,
    SourceDiscoveryRequest,
    SourceDiscoveryResult,
)
from app.services.source_discovery.search_model import (
    SearchDiscoveryUnavailable,
    SearchQueryModel,
)
from app.services.source_ingestion_service import SourceIngestionService
from app.storage.raw_store import LocalRawArtifactStore

# search discovery 覆盖的 source_type（announcement provider 之外的公开资料）。
_SEARCH_SOURCE_TYPES = frozenset({"other", "prospectus"})


def _hostname(url: str) -> str | None:
    """确定性 hostname 提取（校验失败 → None，调用方拒绝该候选）。"""
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    return parsed.hostname.lower().rstrip(".")


def match_provider_domain(
    hostname: str,
    *,
    registry_domains: dict[str, str],
    issuer_domains: set[str],
) -> str | None:
    """hostname → provider_key（纯函数，确定性）。

    - issuer_domains（本公司官网域名）精确命中 → issuer_official；
    - registry allowed_domains 精确或子域（'.domain' 后缀）命中 → 对应
      provider_key；
    - 均未命中 → None（拒绝）。
    """
    host = hostname.lower().rstrip(".")
    if host in issuer_domains:
        return "issuer_official"
    for domain, key in registry_domains.items():
        d = domain.lower().rstrip(".")
        if host == d or host.endswith("." + d):
            return key
    return None


class SearchDiscoveryProvider:
    """受控搜索发现（P2）：LLM 候选 → 域名 allowlist → 抓取验证 → 落库。"""

    provider_key = "search_discovery"

    def __init__(
        self,
        query_model: SearchQueryModel | None = None,
        *,
        sessionmaker: async_sessionmaker | None = None,
        raw_store: LocalRawArtifactStore | None = None,
        fetcher: SafePdfFetcher | None = None,
        max_bytes: int = 104857600,
        ingestion: SourceIngestionService | None = None,
        registry_domains: dict[str, str] | None = None,
    ) -> None:
        self._query_model = query_model
        self._sessionmaker = sessionmaker
        self._raw_store = raw_store
        self._fetcher = fetcher
        self._max_bytes = max_bytes
        self._ingestion = ingestion
        # 构造时注入的 registry 域名映射（测试用）；None → 惰性从 DB 加载。
        self._registry_domains = registry_domains

    def supports(self, request: SourceDiscoveryRequest) -> bool:
        if request.need_kind == "document":
            return request.source_type in _SEARCH_SOURCE_TYPES
        # 行业 / 监管等非文档资料（P2 扩展）。
        return request.need_kind in ("financial", "valuation")

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryResult:
        if self._query_model is None:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_SEARCH_NOT_CONFIGURED,
                exhausted=True,
            )
        if self._ingestion is None and (self._sessionmaker is None or self._raw_store is None):
            # 未装配落库依赖（unit 测试）→ 不伪装可用。
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_SEARCH_NOT_CONFIGURED,
                exhausted=True,
            )
        try:
            output = await self._query_model.generate(request)
        except SearchDiscoveryUnavailable:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_SEARCH_NOT_CONFIGURED,
                exhausted=True,
            )
        except Exception:  # noqa: BLE001 - 契约违反：不泄漏
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )
        if not output.candidates:
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )

        try:
            registry_domains = await self._load_registry_domains()
            issuer_domains = await self._load_issuer_domains(request.company_id)
            ingestion = self._ingestion or SourceIngestionService(
                self._sessionmaker,
                self._raw_store,
                fetcher=self._fetcher,
                max_bytes=self._max_bytes,
            )
        except Exception:  # noqa: BLE001 - 依赖加载失败 → exhausted
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=False,
                reason=REASON_NO_CANDIDATES,
                exhausted=True,
            )

        document_type = self._document_type(request)
        for candidate in output.candidates:
            hostname = _hostname(candidate.url)
            if hostname is None:
                continue
            provider_key = match_provider_domain(
                hostname,
                registry_domains=registry_domains,
                issuer_domains=issuer_domains,
            )
            if provider_key is None:
                # 域名不在受控 allowlist 内 → 拒绝（不抓取、不落库）。
                continue
            try:
                result = await ingestion.ingest_discovered(
                    company_id=request.company_id,
                    provider_key=provider_key,
                    document_type=document_type,
                    title=candidate.title[:500],
                    source_url=candidate.url,
                    published_at=None,
                    reporting_period_end=None,
                    external_document_id=None,
                )
            except Exception:  # noqa: BLE001 - 单个候选失败 → 尝试下一个
                continue
            if result.replayed:
                continue  # 已存在相同来源 → 尝试下一个候选
            return SourceDiscoveryResult(
                provider_key=self.provider_key,
                acquired=True,
                source_ids=(result.record.source_id,),
            )
        return SourceDiscoveryResult(
            provider_key=self.provider_key,
            acquired=False,
            reason=REASON_NO_CANDIDATES,
            exhausted=True,
        )

    # ------------------------------------------------------------ internal

    @staticmethod
    def _document_type(request: SourceDiscoveryRequest) -> SourceDocumentType:
        if request.source_type == "prospectus":
            return SourceDocumentType.PROSPECTUS
        return SourceDocumentType.OTHER

    async def _load_registry_domains(self) -> dict[str, str]:
        """enabled provider 的 allowed_domains → provider_key 映射（惰性加载）。"""
        if self._registry_domains is not None:
            return self._registry_domains
        if self._sessionmaker is None:
            return {}
        async with self._sessionmaker() as session:
            rows = await SourceProviderRepository(session).list_providers(enabled_only=True)
        mapping: dict[str, str] = {}
        for row in rows:
            for domain in row.allowed_domains or []:
                mapping.setdefault(domain.lower(), row.provider_key)
        return mapping

    async def _load_issuer_domains(self, company_id: UUID) -> set[str]:
        """本公司的 issuer_domains 官网域名（company 绑定，不允许任意网站伪装）。"""
        if self._sessionmaker is None:
            return set()
        async with self._sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        select(IssuerDomainModel.domain).where(
                            IssuerDomainModel.company_id == company_id
                        )
                    )
                )
                .scalars()
                .all()
            )
        return {str(d).lower() for d in rows}
