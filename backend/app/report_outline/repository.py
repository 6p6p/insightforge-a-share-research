"""Data access for deterministic report outlines (stage 5A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.report_outline import ReportOutlineModel


class ReportOutlineRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, outline_id: UUID) -> ReportOutlineModel | None:
        result = await self._session.execute(
            select(ReportOutlineModel).where(ReportOutlineModel.outline_id == outline_id)
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, outline_fingerprint: str) -> ReportOutlineModel | None:
        result = await self._session.execute(
            select(ReportOutlineModel).where(
                ReportOutlineModel.outline_fingerprint == outline_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, outline: ReportOutlineModel) -> tuple[ReportOutlineModel, bool]:
        """INSERT ... ON CONFLICT(outline_fingerprint) DO NOTHING RETURNING。

        并发下相同提纲（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（SynthesisResult / schema / payload 任一变化 = 新指纹 = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(outline, column.key)
            for column in ReportOutlineModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ReportOutlineModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ReportOutlineModel.outline_fingerprint])
            .returning(ReportOutlineModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(outline.outline_fingerprint)
        if existing is None:
            raise RuntimeError("report outline conflict without existing row")
        return existing, False
