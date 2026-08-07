"""Data access for macro dataset snapshots and artifact links (stage 2C.2A)."""

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_snapshot_artifact import MacroSnapshotArtifactModel


class MacroSnapshotRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers.

    Snapshot / ArtifactLink 都是不可变记录：不提供 update 方法。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, snapshot_id: UUID) -> MacroDatasetSnapshotModel | None:
        result = await self._session.execute(
            select(MacroDatasetSnapshotModel).where(
                MacroDatasetSnapshotModel.snapshot_id == snapshot_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, fingerprint: str) -> MacroDatasetSnapshotModel | None:
        result = await self._session.execute(
            select(MacroDatasetSnapshotModel).where(
                MacroDatasetSnapshotModel.snapshot_fingerprint == fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create(self, snapshot: MacroDatasetSnapshotModel) -> MacroDatasetSnapshotModel:
        # 快照不可变：仅插入。snapshot_fingerprint 唯一约束在并发重复时抛
        # IntegrityError，由调用方决定处理。
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

    async def create_or_get_by_fingerprint(
        self,
        snapshot: MacroDatasetSnapshotModel,
    ) -> tuple[MacroDatasetSnapshotModel, bool]:
        """INSERT ... ON CONFLICT(snapshot_fingerprint) DO NOTHING RETURNING。

        并发下只有赢得 insert 的事务会返回行（created=True）；输掉的一方
        回查既有行（created=False），且**不得**再插入 Links/Observations
        （由 Service 依据 created 标志决定）。created_at 依赖 server_default，
        不在 values 中显式赋值。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(snapshot, column.key)
            for column in MacroDatasetSnapshotModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(MacroDatasetSnapshotModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[MacroDatasetSnapshotModel.snapshot_fingerprint]
            )
            .returning(MacroDatasetSnapshotModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(snapshot.snapshot_fingerprint)
        if existing is None:
            raise RuntimeError("fingerprint conflict without existing row")
        return existing, False

    async def list_for_series(
        self,
        series_id: UUID,
        *,
        limit: int,
        offset: int,
    ) -> list[MacroDatasetSnapshotModel]:
        # 稳定排序：fetched_at DESC, created_at DESC, snapshot_id ASC
        result = await self._session.execute(
            select(MacroDatasetSnapshotModel)
            .where(MacroDatasetSnapshotModel.series_id == series_id)
            .order_by(
                MacroDatasetSnapshotModel.fetched_at.desc(),
                MacroDatasetSnapshotModel.created_at.desc(),
                MacroDatasetSnapshotModel.snapshot_id.asc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def count_for_series(self, series_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(MacroDatasetSnapshotModel)
            .where(MacroDatasetSnapshotModel.series_id == series_id)
        )
        return int(result.scalar_one())

    async def get_latest_for_series(self, series_id: UUID) -> MacroDatasetSnapshotModel | None:
        result = await self._session.execute(
            select(MacroDatasetSnapshotModel)
            .where(MacroDatasetSnapshotModel.series_id == series_id)
            .order_by(
                MacroDatasetSnapshotModel.fetched_at.desc(),
                MacroDatasetSnapshotModel.created_at.desc(),
                MacroDatasetSnapshotModel.snapshot_id.desc(),
            )
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def add_artifact_link(
        self, link: MacroSnapshotArtifactModel
    ) -> MacroSnapshotArtifactModel:
        self._session.add(link)
        await self._session.flush()
        return link

    async def list_artifact_links(self, snapshot_id: UUID) -> list[MacroSnapshotArtifactModel]:
        # 稳定排序：role 升序 + page 升序（元数据 role page=NULL 用 NULLS FIRST）。
        result = await self._session.execute(
            select(MacroSnapshotArtifactModel)
            .where(MacroSnapshotArtifactModel.snapshot_id == snapshot_id)
            .order_by(
                MacroSnapshotArtifactModel.role.asc(),
                MacroSnapshotArtifactModel.page.asc().nulls_first(),
            )
        )
        return list(result.scalars().all())

    async def count_artifact_links(self, snapshot_id: UUID) -> int:
        result = await self._session.execute(
            select(func.count())
            .select_from(MacroSnapshotArtifactModel)
            .where(MacroSnapshotArtifactModel.snapshot_id == snapshot_id)
        )
        return int(result.scalar_one())
