"""Data access for evidence-bound section drafts (stage 5B)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.draft_section import DraftSectionModel


class DraftSectionRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, draft_section_id: UUID) -> DraftSectionModel | None:
        result = await self._session.execute(
            select(DraftSectionModel).where(DraftSectionModel.draft_section_id == draft_section_id)
        )
        return result.scalar_one_or_none()

    async def get_by_writer_input_fingerprint(
        self, writer_input_fingerprint: str
    ) -> DraftSectionModel | None:
        result = await self._session.execute(
            select(DraftSectionModel).where(
                DraftSectionModel.writer_input_fingerprint == writer_input_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, draft: DraftSectionModel) -> tuple[DraftSectionModel, bool]:
        """INSERT ... ON CONFLICT(writer_input_fingerprint) DO NOTHING RETURNING。

        并发下相同 writer 输入（同一指纹）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（输入 / schema / writer 任一变化 = 新指纹 = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(draft, column.key)
            for column in DraftSectionModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(DraftSectionModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[DraftSectionModel.writer_input_fingerprint])
            .returning(DraftSectionModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_writer_input_fingerprint(draft.writer_input_fingerprint)
        if existing is None:
            raise RuntimeError("draft section conflict without existing row")
        return existing, False
