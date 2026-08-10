"""Data access for claim synthesis results (stage 4D.1B)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim_synthesis_result import ClaimSynthesisResultModel


class ClaimSynthesisResultRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, synthesis_result_id: UUID) -> ClaimSynthesisResultModel | None:
        result = await self._session.execute(
            select(ClaimSynthesisResultModel).where(
                ClaimSynthesisResultModel.synthesis_result_id == synthesis_result_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, result_fingerprint: str) -> ClaimSynthesisResultModel | None:
        result = await self._session.execute(
            select(ClaimSynthesisResultModel).where(
                ClaimSynthesisResultModel.result_fingerprint == result_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def list_by_synthesis(self, synthesis_id: UUID) -> list[ClaimSynthesisResultModel]:
        result = await self._session.execute(
            select(ClaimSynthesisResultModel).where(
                ClaimSynthesisResultModel.synthesis_id == synthesis_id
            )
        )
        return list(result.scalars().all())

    async def create_or_get(
        self, result: ClaimSynthesisResultModel
    ) -> tuple[ClaimSynthesisResultModel, bool]:
        """INSERT ... ON CONFLICT(result_fingerprint) DO NOTHING RETURNING。

        并发下相同结果（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（run / analyst / 输出任一变化 = 新指纹 = 新结果）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(result, column.key)
            for column in ClaimSynthesisResultModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ClaimSynthesisResultModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ClaimSynthesisResultModel.result_fingerprint])
            .returning(ClaimSynthesisResultModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(result.result_fingerprint)
        if existing is None:
            raise RuntimeError("synthesis result conflict without existing row")
        return existing, False
