"""Data access for evidence-bound section revisions (stage 5E.2A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.draft_section_revision import DraftSectionRevisionModel


class RevisionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, revision_id: UUID) -> DraftSectionRevisionModel | None:
        result = await self._session.execute(
            select(DraftSectionRevisionModel).where(
                DraftSectionRevisionModel.revision_id == revision_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_revision_fingerprint(
        self, revision_fingerprint: str
    ) -> DraftSectionRevisionModel | None:
        result = await self._session.execute(
            select(DraftSectionRevisionModel).where(
                DraftSectionRevisionModel.revision_fingerprint == revision_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def get_by_revised_draft_section_id(
        self, revised_draft_section_id: UUID
    ) -> DraftSectionRevisionModel | None:
        """一条修订输出的 DraftSection 恰有一条 revision link（UNIQUE）。"""
        result = await self._session.execute(
            select(DraftSectionRevisionModel).where(
                DraftSectionRevisionModel.revised_draft_section_id == revised_draft_section_id
            )
        )
        return result.scalar_one_or_none()

    async def list_by_source_draft_section_id(
        self, source_draft_section_id: UUID
    ) -> list[DraftSectionRevisionModel]:
        """一个 source draft 的修订链（按 created_at 升序，Stage5 loop 恢复用）。"""
        result = await self._session.execute(
            select(DraftSectionRevisionModel)
            .where(DraftSectionRevisionModel.source_draft_section_id == source_draft_section_id)
            .order_by(DraftSectionRevisionModel.created_at)
        )
        return list(result.scalars().all())

    async def create_or_get(
        self, revision: DraftSectionRevisionModel
    ) -> tuple[DraftSectionRevisionModel, bool]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING（无目标索引）。

        并发下相同修订输入（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（source / trigger / feedback 任一变化 = 新指纹 = 新行）。

        注意：不用 `ON CONFLICT (revision_fingerprint)`，因为模型同时有
        `uq_draft_section_revisions_revised_draft_section_id` 与
        `uq_draft_section_revisions_revision_fingerprint` 两个唯一约束。无目标索引的
        `ON CONFLICT DO NOTHING` 抑制**任意**唯一约束冲突，随后回查既有行即得真实行
        （镜像 DraftSectionRepository 的并发模式）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(revision, column.key)
            for column in DraftSectionRevisionModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(DraftSectionRevisionModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(DraftSectionRevisionModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_revision_fingerprint(revision.revision_fingerprint)
        if existing is None:
            raise RuntimeError("draft section revision conflict without existing row")
        return existing, False
