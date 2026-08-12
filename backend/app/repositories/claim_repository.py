"""Data access for claims (stage 4A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim import ClaimModel


class ClaimRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, claim_id: UUID) -> ClaimModel | None:
        result = await self._session.execute(
            select(ClaimModel).where(ClaimModel.claim_id == claim_id)
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, claim_fingerprint: str) -> ClaimModel | None:
        result = await self._session.execute(
            select(ClaimModel).where(ClaimModel.claim_fingerprint == claim_fingerprint)
        )
        return result.scalar_one_or_none()

    async def list_by_ids(self, claim_ids: list[UUID]) -> list[ClaimModel]:
        """按 id 批量加载（补充研究计划派生 related Claim statements 用）。"""
        if not claim_ids:
            return []
        result = await self._session.execute(
            select(ClaimModel).where(ClaimModel.claim_id.in_(claim_ids))
        )
        return list(result.scalars().all())

    async def list_by_company(
        self,
        company_id: UUID,
        limit: int,
        offset: int,
    ) -> list[ClaimModel]:
        stmt = (
            select(ClaimModel)
            .where(ClaimModel.company_id == company_id)
            .order_by(ClaimModel.created_at.asc(), ClaimModel.claim_id.asc())
            .limit(limit)
            .offset(offset)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def create_or_get(self, claim: ClaimModel) -> tuple[ClaimModel, bool]:
        """INSERT ... ON CONFLICT(claim_fingerprint) DO NOTHING RETURNING。

        并发下相同 Claim（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（修改观点 = 新 Claim = 新 fingerprint = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(claim, column.key)
            for column in ClaimModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ClaimModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ClaimModel.claim_fingerprint])
            .returning(ClaimModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(claim.claim_fingerprint)
        if existing is None:
            raise RuntimeError("claim conflict without existing row")
        return existing, False
