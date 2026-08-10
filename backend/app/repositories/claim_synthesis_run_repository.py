"""Data access for claim synthesis runs (stage 4D.1A)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.claim_synthesis_run import ClaimSynthesisRunModel


class ClaimSynthesisRunRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, synthesis_id: UUID) -> ClaimSynthesisRunModel | None:
        result = await self._session.execute(
            select(ClaimSynthesisRunModel).where(
                ClaimSynthesisRunModel.synthesis_id == synthesis_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, synthesis_fingerprint: str) -> ClaimSynthesisRunModel | None:
        result = await self._session.execute(
            select(ClaimSynthesisRunModel).where(
                ClaimSynthesisRunModel.synthesis_fingerprint == synthesis_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, run: ClaimSynthesisRunModel
    ) -> tuple[ClaimSynthesisRunModel, bool]:
        """INSERT ... ON CONFLICT(synthesis_fingerprint) DO NOTHING RETURNING。

        并发下相同输入（同一 fingerprint）只能有 1 个 run：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（输入任一变化 = 新指纹 = 新 run）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(run, column.key)
            for column in ClaimSynthesisRunModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ClaimSynthesisRunModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ClaimSynthesisRunModel.synthesis_fingerprint])
            .returning(ClaimSynthesisRunModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(run.synthesis_fingerprint)
        if existing is None:
            raise RuntimeError("synthesis run conflict without existing row")
        return existing, False
