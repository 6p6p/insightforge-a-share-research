"""Data access for company identities and aliases."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.company import CompanyModel
from app.db.models.company_alias import CompanyAliasModel


class CompanyRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, company: CompanyModel) -> CompanyModel:
        self._session.add(company)
        await self._session.flush()
        return company

    async def add_alias(self, alias: CompanyAliasModel) -> CompanyAliasModel:
        self._session.add(alias)
        await self._session.flush()
        return alias

    async def get_by_id(self, company_id: UUID) -> CompanyModel | None:
        result = await self._session.execute(
            select(CompanyModel).where(CompanyModel.company_id == company_id)
        )
        return result.scalar_one_or_none()

    async def get_by_identity_key(self, identity_key: str) -> CompanyModel | None:
        result = await self._session.execute(
            select(CompanyModel).where(CompanyModel.identity_key == identity_key)
        )
        return result.scalar_one_or_none()

    async def find_by_security_code(self, security_code: str) -> list[CompanyModel]:
        result = await self._session.execute(
            select(CompanyModel)
            .where(CompanyModel.security_code == security_code)
            .order_by(CompanyModel.exchange.asc(), CompanyModel.company_id.asc())
        )
        return list(result.scalars().all())

    async def find_by_normalized_alias(
        self,
        normalized_alias: str,
    ) -> list[tuple[CompanyModel, str]]:
        result = await self._session.execute(
            select(CompanyModel, CompanyAliasModel.alias_type)
            .join(
                CompanyAliasModel,
                CompanyAliasModel.company_id == CompanyModel.company_id,
            )
            .where(CompanyAliasModel.normalized_alias == normalized_alias)
            .order_by(CompanyModel.exchange.asc(), CompanyModel.company_id.asc())
        )
        return [(company, alias_type) for company, alias_type in result.all()]
