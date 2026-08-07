"""Data access for source records."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
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

    async def create_or_get(
        self,
        record: SourceRecordModel,
    ) -> tuple[SourceRecordModel, bool]:
        """INSERT ... ON CONFLICT(provider_key, source_url, artifact_id) DO NOTHING RETURNING。

        并发下多个 Candidate 可能命中同一 final_url + artifact 内容：只有赢家
        created=True，输家回查既有 SourceRecord 复用（不同 Candidate 各自独立
        Verification，见 ADR-0015 不变量 H / §二十五 B）。
        """
        stmt = (
            insert(SourceRecordModel)
            .values(
                company_id=record.company_id,
                provider_key=record.provider_key,
                artifact_id=record.artifact_id,
                document_type=record.document_type,
                title=record.title,
                published_at=record.published_at,
                reporting_period_end=record.reporting_period_end,
                source_url=record.source_url,
                acquisition_method=record.acquisition_method,
                external_document_id=record.external_document_id,
                authority_tier_snapshot=record.authority_tier_snapshot,
                critical_claim_eligible_snapshot=record.critical_claim_eligible_snapshot,
                provider_capabilities_snapshot=record.provider_capabilities_snapshot,
                status=record.status,
                acquired_at=record.acquired_at,
            )
            .on_conflict_do_nothing(
                index_elements=[
                    "provider_key",
                    "source_url",
                    "artifact_id",
                ]
            )
            .returning(SourceRecordModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.find_existing(
            record.provider_key,
            record.source_url,
            record.artifact_id,
        )
        if existing is None:
            raise RuntimeError("source record conflict without existing row")
        return existing, False

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
