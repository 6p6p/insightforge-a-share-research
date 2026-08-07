"""Data access for raw artifacts."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import RawArtifactNotFound
from app.db.models.raw_artifact import RawArtifactModel


class RawArtifactRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_sha256(self, content_sha256: str) -> RawArtifactModel | None:
        result = await self._session.execute(
            select(RawArtifactModel).where(RawArtifactModel.content_sha256 == content_sha256)
        )
        return result.scalar_one_or_none()

    async def get_by_id(self, artifact_id: UUID) -> RawArtifactModel | None:
        result = await self._session.execute(
            select(RawArtifactModel).where(RawArtifactModel.artifact_id == artifact_id)
        )
        return result.scalar_one_or_none()

    async def create(self, artifact: RawArtifactModel) -> RawArtifactModel | None:
        """Insert with ON CONFLICT DO NOTHING for concurrent dedup.

        content_sha256 冲突时返回 None（调用方需回查已有行），
        保证两个并发相同文件导入最终只有一条 raw_artifacts 记录。
        """
        stmt = (
            insert(RawArtifactModel)
            .values(
                content_sha256=artifact.content_sha256,
                storage_key=artifact.storage_key,
                byte_size=artifact.byte_size,
                media_type=artifact.media_type,
            )
            .on_conflict_do_nothing(index_elements=[RawArtifactModel.content_sha256])
            .returning(RawArtifactModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_or_create(
        self,
        artifact: RawArtifactModel,
    ) -> tuple[RawArtifactModel, bool]:
        """并发安全插入：冲突返回既有行。created=True 表示本次插入成功。"""
        created = await self.create(artifact)
        if created is not None:
            return created, True
        existing = await self.get_by_sha256(artifact.content_sha256)
        if existing is None:
            raise RawArtifactNotFound()
        return existing, False
