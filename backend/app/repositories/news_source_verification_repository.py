"""Data access for news source verifications (stage 2D.2A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.news_source_verification import NewsSourceVerificationModel


class NewsSourceVerificationRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_candidate_id(
        self,
        candidate_id: UUID,
    ) -> NewsSourceVerificationModel | None:
        result = await self._session.execute(
            select(NewsSourceVerificationModel).where(
                NewsSourceVerificationModel.candidate_id == candidate_id
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get_by_candidate(
        self,
        verification: NewsSourceVerificationModel,
    ) -> tuple[NewsSourceVerificationModel, bool]:
        """INSERT ... ON CONFLICT(candidate_id) DO NOTHING RETURNING。

        并发下同一 candidate 只允许一条 Verification；输掉的一方回查既有行
        （created=False），保证并发 verify_candidate 返回同一 verification_id。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(verification, column.key)
            for column in NewsSourceVerificationModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(NewsSourceVerificationModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[NewsSourceVerificationModel.candidate_id]
            )
            .returning(NewsSourceVerificationModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_candidate_id(verification.candidate_id)
        if existing is None:
            raise RuntimeError("verification conflict without existing row")
        return existing, False

    async def get_by_id(self, verification_id: UUID) -> NewsSourceVerificationModel | None:
        result = await self._session.execute(
            select(NewsSourceVerificationModel).where(
                NewsSourceVerificationModel.verification_id == verification_id
            )
        )
        return result.scalar_one_or_none()
