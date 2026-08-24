"""Data access for user-configured LLM provider configs."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.llm_provider_config import LlmProviderConfigModel


class LlmProviderConfigRepository:
    """Repository scoped to a single AsyncSession; transactions coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, config: LlmProviderConfigModel
    ) -> LlmProviderConfigModel:
        self._session.add(config)
        await self._session.flush()
        await self._session.refresh(config)
        return config

    async def get_by_id(self, config_id: UUID) -> LlmProviderConfigModel | None:
        result = await self._session.execute(
            select(LlmProviderConfigModel).where(
                LlmProviderConfigModel.id == config_id
            )
        )
        return result.scalar_one_or_none()

    async def list_all(
        self,
    ) -> list[LlmProviderConfigModel]:
        result = await self._session.execute(
            select(LlmProviderConfigModel).order_by(
                LlmProviderConfigModel.created_at.asc(),
                LlmProviderConfigModel.id.asc(),
            )
        )
        return list(result.scalars().all())

    async def clear_active(self) -> None:
        rows = await self._session.execute(
            select(LlmProviderConfigModel).where(
                LlmProviderConfigModel.is_active.is_(True)
            )
        )
        for row in rows.scalars().all():
            row.is_active = False
        await self._session.flush()

    async def delete(self, config: LlmProviderConfigModel) -> None:
        await self._session.delete(config)
        await self._session.flush()

    async def flush(self) -> None:
        """显式 flush 挂起变更（update / set_active 修改行属性后调用）。"""
        await self._session.flush()
