"""Data access for news discovery candidates (stage 2D.1)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.news_discovery_candidate import NewsDiscoveryCandidateModel


class NewsDiscoveryCandidateRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers.

    Candidate 绑定 Run 且不可变：只支持批量插入与查询，不提供 update。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def bulk_create(self, candidates: list[NewsDiscoveryCandidateModel]) -> int:
        if not candidates:
            return 0
        self._session.add_all(candidates)
        await self._session.flush()
        return len(candidates)

    async def get_by_id(self, candidate_id: UUID) -> NewsDiscoveryCandidateModel | None:
        result = await self._session.execute(
            select(NewsDiscoveryCandidateModel).where(
                NewsDiscoveryCandidateModel.candidate_id == candidate_id
            )
        )
        return result.scalar_one_or_none()

    async def list_for_run(self, discovery_run_id: UUID) -> list[NewsDiscoveryCandidateModel]:
        # 稳定排序：rank ASC, candidate_id ASC
        result = await self._session.execute(
            select(NewsDiscoveryCandidateModel)
            .where(NewsDiscoveryCandidateModel.discovery_run_id == discovery_run_id)
            .order_by(
                NewsDiscoveryCandidateModel.rank.asc(),
                NewsDiscoveryCandidateModel.candidate_id.asc(),
            )
        )
        return list(result.scalars().all())

    async def count_for_run(self, discovery_run_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(NewsDiscoveryCandidateModel)
            .where(NewsDiscoveryCandidateModel.discovery_run_id == discovery_run_id)
        )
        return int(result.scalar_one())
