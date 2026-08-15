"""Source registry service."""

from dataclasses import dataclass
from urllib.parse import urlparse
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import SourceProviderNotFound, SourceUrlNotAllowed
from app.db.models.source_provider import SourceProviderModel
from app.domain.companies import ExchangeCode
from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.issuer_domain_service import IssuerDomainService
from app.source_registry.defaults import DEFAULT_PROVIDERS
from app.source_registry.url_policy import is_url_allowed

# resolve_provider_for_url 只考虑能承接公司文档的来源。
_DOCUMENT_CAPABILITIES = frozenset(
    {
        SourceCapability.COMPANY_ANNOUNCEMENT.value,
        SourceCapability.ISSUER_IR.value,
        SourceCapability.DOCUMENT_DOWNLOAD.value,
    }
)


@dataclass(frozen=True)
class ResolvedProvider:
    """URL → provider 的确定性解析结果（V1.1 closure）。"""

    provider_key: str
    display_name: str
    authority_tier: int
    critical_claim_eligible: bool
    matched_by: str  # "issuer_domain" | "allowed_domain"


@dataclass
class SourceSeedResult:
    inserted_or_updated: int
    total: int


def _definition_to_model(definition) -> SourceProviderModel:
    return SourceProviderModel(
        provider_key=definition.provider_key,
        display_name=definition.display_name,
        provider_type=definition.provider_type.value,
        authority_tier=int(definition.authority_tier),
        homepage_url=definition.homepage_url,
        allowed_domains=definition.allowed_domains,
        capabilities=[c.value for c in definition.capabilities],
        acquisition_methods=[m.value for m in definition.acquisition_methods],
        exchange_scope=definition.exchange_scope,
        requires_api_key=definition.requires_api_key,
        critical_claim_eligible=definition.critical_claim_eligible,
        enabled=definition.enabled,
    )


class SourceRegistryService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def seed_defaults(self) -> SourceSeedResult:
        # 默认定义已在模块加载时完成结构校验；此处再次防御性遍历
        for definition in DEFAULT_PROVIDERS:
            if not definition.provider_key:
                raise ValueError("empty provider key")
        async with self._sessionmaker() as session:
            repo = SourceProviderRepository(session)
            for definition in DEFAULT_PROVIDERS:
                await repo.upsert(_definition_to_model(definition))
            await session.commit()
            total = await repo.count()
        return SourceSeedResult(
            inserted_or_updated=len(DEFAULT_PROVIDERS),
            total=total,
        )

    async def get_provider(self, provider_key: str) -> SourceProviderModel:
        async with self._sessionmaker() as session:
            provider = await SourceProviderRepository(session).get_by_key(provider_key)
        if provider is None:
            raise SourceProviderNotFound()
        return provider

    async def list_providers(
        self,
        *,
        authority_tier: SourceAuthorityTier | None = None,
        capability: SourceCapability | None = None,
        acquisition_method: AcquisitionMethod | None = None,
        exchange: ExchangeCode | None = None,
        enabled_only: bool = True,
    ) -> list[SourceProviderModel]:
        async with self._sessionmaker() as session:
            return await SourceProviderRepository(session).list_providers(
                authority_tier=authority_tier,
                capability=capability,
                acquisition_method=acquisition_method,
                exchange=exchange,
                enabled_only=enabled_only,
            )

    async def validate_source_url(self, provider_key: str, url: str) -> None:
        async with self._sessionmaker() as session:
            provider = await SourceProviderRepository(session).get_by_key(provider_key)
        if provider is None:
            raise SourceProviderNotFound()
        if not is_url_allowed(url, provider.allowed_domains):
            raise SourceUrlNotAllowed()

    async def resolve_provider_for_url(
        self, company_id: UUID, url: str
    ) -> ResolvedProvider:
        """URL → provider 的确定性自动解析（V1.1 closure）。

        1. issuer_domains registry：URL hostname 匹配该公司登记的官网域名 →
           `issuer_official`（公司官网，company_id 绑定）；
        2. provider allowlist：hostname 匹配某个 enabled 且具备文档能力的
           provider 的 allowed_domains → 该 provider；
        3. 都不匹配 → `SourceUrlNotAllowed`（前端提示用户手动选择或改 URL）。

        **不降低现有 allowlist / SSRF 策略**：issuer_official 只对 registry
        内登记的域名生效；URL 本身从不被本方法抓取。
        """
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise SourceUrlNotAllowed()
        if parsed.username is not None or parsed.password is not None:
            raise SourceUrlNotAllowed()
        if parsed.port is not None:
            raise SourceUrlNotAllowed()

        issuer_service = IssuerDomainService(self._sessionmaker)
        if await issuer_service.match_issuer_url(company_id, url):
            async with self._sessionmaker() as session:
                provider = await SourceProviderRepository(session).get_by_key("issuer_official")
            if provider is not None:
                return ResolvedProvider(
                    provider_key=provider.provider_key,
                    display_name=provider.display_name,
                    authority_tier=int(provider.authority_tier),
                    critical_claim_eligible=provider.critical_claim_eligible,
                    matched_by="issuer_domain",
                )

        async with self._sessionmaker() as session:
            providers = await SourceProviderRepository(session).list_providers(
                authority_tier=None,
                capability=None,
                acquisition_method=None,
                exchange=None,
                enabled_only=True,
            )
        for provider in providers:
            if not (set(provider.capabilities) & _DOCUMENT_CAPABILITIES):
                continue
            if is_url_allowed(url, provider.allowed_domains):
                return ResolvedProvider(
                    provider_key=provider.provider_key,
                    display_name=provider.display_name,
                    authority_tier=int(provider.authority_tier),
                    critical_claim_eligible=provider.critical_claim_eligible,
                    matched_by="allowed_domain",
                )
        raise SourceUrlNotAllowed()
