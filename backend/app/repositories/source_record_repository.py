"""Data access for source records."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.source_record import SourceRecordModel


class SourceRecordRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, record: SourceRecordModel) -> SourceRecordModel:
        self._session.add(record)
        await self._session.flush()
        return record

    async def get_by_id(self, source_id: UUID) -> SourceRecordModel | None:
        result = await self._session.execute(
            select(SourceRecordModel).where(SourceRecordModel.source_id == source_id)
        )
        return result.scalar_one_or_none()

    async def find_existing(
        self,
        provider_key: str,
        source_url: str,
        artifact_id: UUID,
    ) -> SourceRecordModel | None:
        result = await self._session.execute(
            select(SourceRecordModel).where(
                SourceRecordModel.provider_key == provider_key,
                SourceRecordModel.source_url == source_url,
                SourceRecordModel.artifact_id == artifact_id,
            )
        )
        return result.scalar_one_or_none()

    async def list_for_company(
        self,
        company_id: UUID,
        document_type: str | None,
        limit: int,
        offset: int,
    ) -> list[SourceRecordModel]:
        stmt = select(SourceRecordModel).where(SourceRecordModel.company_id == company_id)
        if document_type is not None:
            stmt = stmt.where(SourceRecordModel.document_type == document_type)
        stmt = (
            stmt.order_by(
                SourceRecordModel.published_at.desc().nulls_last(),
                SourceRecordModel.created_at.desc(),
                SourceRecordModel.source_id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def count_for_company(
        self,
        company_id: UUID,
        document_type: str | None,
    ) -> int:
        stmt = (
            select(func.count())
            .select_from(SourceRecordModel)
            .where(SourceRecordModel.company_id == company_id)
        )
        if document_type is not None:
            stmt = stmt.where(SourceRecordModel.document_type == document_type)
        result = await self._session.execute(stmt)
        return int(result.scalar_one())
