"""Data access for macro dataset snapshots and artifact links (stage 2C.2A)."""

from uuid import UUID

from sqlalchemy import func, select
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
        # IntegrityError，由调用方（后续 2C.2B Service）决定处理。
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot

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
