"""Data access for relative valuation comparisons (stage 4C.2A)."""

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.relative_valuation_comparison import RelativeValuationComparisonModel


class RelativeValuationComparisonRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, comparison_id: object) -> RelativeValuationComparisonModel | None:
        result = await self._session.execute(
            select(RelativeValuationComparisonModel).where(
                RelativeValuationComparisonModel.comparison_id == comparison_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(
        self, comparison_fingerprint: str
    ) -> RelativeValuationComparisonModel | None:
        result = await self._session.execute(
            select(RelativeValuationComparisonModel).where(
                RelativeValuationComparisonModel.comparison_fingerprint == comparison_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, comparison: RelativeValuationComparisonModel
    ) -> tuple[RelativeValuationComparisonModel, bool]:
        """INSERT ... ON CONFLICT(comparison_fingerprint) DO NOTHING RETURNING。

        并发下相同 Comparison（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义，同时回查 peer links 完整性）。
        **无 Python 进程锁**；不允许 update API。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(comparison, column.key)
            for column in RelativeValuationComparisonModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(RelativeValuationComparisonModel)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[RelativeValuationComparisonModel.comparison_fingerprint]
            )
            .returning(RelativeValuationComparisonModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(comparison.comparison_fingerprint)
        if existing is None:
            raise RuntimeError("relative valuation comparison conflict without existing row")
        return existing, False
