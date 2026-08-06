"""Source registry service."""

from dataclasses import dataclass

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
from app.source_registry.defaults import DEFAULT_PROVIDERS
from app.source_registry.url_policy import is_url_allowed


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
