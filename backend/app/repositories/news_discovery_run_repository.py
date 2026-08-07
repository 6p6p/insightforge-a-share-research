"""Data access for news discovery runs (stage 2D.1)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.news_discovery_run import NewsDiscoveryRunModel


class NewsDiscoveryRunRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers.

    Run 不可变：只支持插入与查询，不提供 update。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, discovery_run_id: UUID) -> NewsDiscoveryRunModel | None:
        result = await self._session.execute(
            select(NewsDiscoveryRunModel).where(
                NewsDiscoveryRunModel.discovery_run_id == discovery_run_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, query_fingerprint: str) -> NewsDiscoveryRunModel | None:
        result = await self._session.execute(
            select(NewsDiscoveryRunModel).where(
                NewsDiscoveryRunModel.query_fingerprint == query_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get_by_fingerprint(
        self,
        run: NewsDiscoveryRunModel,
    ) -> tuple[NewsDiscoveryRunModel, bool]:
        """INSERT ... ON CONFLICT(query_fingerprint) DO NOTHING RETURNING。

        并发下只有赢得 insert 的事务会返回行（created=True）；输掉的一方
        回查既有行（created=False），且**不得**再插入 Candidates（由 Service
        依据 created 标志决定）。created_at 依赖 server_default，不在 values
        中显式赋值。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(run, column.key)
            for column in NewsDiscoveryRunModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(NewsDiscoveryRunModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[NewsDiscoveryRunModel.query_fingerprint])
            .returning(NewsDiscoveryRunModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(run.query_fingerprint)
        if existing is None:
            raise RuntimeError("fingerprint conflict without existing row")
        return existing, False

    async def list_for_company(
        self,
        company_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[NewsDiscoveryRunModel]:
        # 稳定排序：fetched_at DESC, created_at DESC, discovery_run_id ASC
        result = await self._session.execute(
            select(NewsDiscoveryRunModel)
            .where(NewsDiscoveryRunModel.company_id == company_id)
            .order_by(
                NewsDiscoveryRunModel.fetched_at.desc(),
                NewsDiscoveryRunModel.created_at.desc(),
                NewsDiscoveryRunModel.discovery_run_id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_company(self, company_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(NewsDiscoveryRunModel)
            .where(NewsDiscoveryRunModel.company_id == company_id)
        )
        return int(result.scalar_one())
