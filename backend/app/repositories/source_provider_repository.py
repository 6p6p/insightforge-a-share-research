"""Data access for source providers."""

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.source_provider import SourceProviderModel
from app.domain.companies import ExchangeCode
from app.domain.sources import (
    AcquisitionMethod,
    SourceAuthorityTier,
    SourceCapability,
)


class SourceProviderRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def upsert(self, provider: SourceProviderModel) -> None:
        stmt = insert(SourceProviderModel).values(
            provider_key=provider.provider_key,
            display_name=provider.display_name,
            provider_type=provider.provider_type,
            authority_tier=provider.authority_tier,
            homepage_url=provider.homepage_url,
            allowed_domains=provider.allowed_domains,
            capabilities=provider.capabilities,
            acquisition_methods=provider.acquisition_methods,
            exchange_scope=provider.exchange_scope,
            requires_api_key=provider.requires_api_key,
            critical_claim_eligible=provider.critical_claim_eligible,
            enabled=provider.enabled,
        )
        stmt = stmt.on_conflict_do_update(
            constraint="source_providers_pkey",
            set_={
                "display_name": stmt.excluded.display_name,
                "provider_type": stmt.excluded.provider_type,
                "authority_tier": stmt.excluded.authority_tier,
                "homepage_url": stmt.excluded.homepage_url,
                "allowed_domains": stmt.excluded.allowed_domains,
                "capabilities": stmt.excluded.capabilities,
                "acquisition_methods": stmt.excluded.acquisition_methods,
                "exchange_scope": stmt.excluded.exchange_scope,
                "requires_api_key": stmt.excluded.requires_api_key,
                "critical_claim_eligible": stmt.excluded.critical_claim_eligible,
                "enabled": stmt.excluded.enabled,
                "updated_at": func.now(),
            },
        )
        await self._session.execute(stmt)

    async def get_by_key(self, provider_key: str) -> SourceProviderModel | None:
        result = await self._session.execute(
            select(SourceProviderModel).where(SourceProviderModel.provider_key == provider_key)
        )
        return result.scalar_one_or_none()

    async def list_providers(
        self,
        *,
        authority_tier: SourceAuthorityTier | None,
        capability: SourceCapability | None,
        acquisition_method: AcquisitionMethod | None,
        exchange: ExchangeCode | None,
        enabled_only: bool,
    ) -> list[SourceProviderModel]:
        query = select(SourceProviderModel)
        if enabled_only:
            query = query.where(SourceProviderModel.enabled.is_(True))
        if authority_tier is not None:
            query = query.where(SourceProviderModel.authority_tier == int(authority_tier))
        if capability is not None:
            query = query.where(SourceProviderModel.capabilities.contains([capability.value]))
        if acquisition_method is not None:
            query = query.where(
                SourceProviderModel.acquisition_methods.contains([acquisition_method.value])
            )
        if exchange is not None:
            query = query.where(SourceProviderModel.exchange_scope.contains([exchange.value]))
        query = query.order_by(
            SourceProviderModel.authority_tier.asc(),
            SourceProviderModel.provider_key.asc(),
        )
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def count(self) -> int:
        result = await self._session.execute(select(func.count()).select_from(SourceProviderModel))
        return result.scalar_one()

    async def list_original_publishers(self) -> list[SourceProviderModel]:
        """enabled + news_article + public_html 的 Original Publishers（2D.2A）。

        供 OriginalPublisherResolver 使用；Resolver 内部仍会独立复核资格
        （enabled / news_article / public_html），本方法只是缩小查询范围。
        """
        result = await self._session.execute(
            select(SourceProviderModel)
            .where(SourceProviderModel.enabled.is_(True))
            .where(
                SourceProviderModel.capabilities.contains([SourceCapability.NEWS_ARTICLE.value])
            )
            .where(
                SourceProviderModel.acquisition_methods.contains(
                    [AcquisitionMethod.PUBLIC_HTML.value]
                )
            )
            .order_by(
                SourceProviderModel.authority_tier.asc(),
                SourceProviderModel.provider_key.asc(),
            )
        )
        return list(result.scalars().all())
