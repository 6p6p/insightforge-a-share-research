"""Data access for deterministic reports + check results (stage 5C)."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.report import ReportCheckResultModel, ReportModel


class ReportRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, report_id: UUID) -> ReportModel | None:
        result = await self._session.execute(
            select(ReportModel).where(ReportModel.report_id == report_id)
        )
        return result.scalar_one_or_none()

    async def get_by_fingerprint(self, report_fingerprint: str) -> ReportModel | None:
        result = await self._session.execute(
            select(ReportModel).where(ReportModel.report_fingerprint == report_fingerprint)
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, report: ReportModel) -> tuple[ReportModel, bool]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING（无目标索引）。

        并发下相同装配（同一 fingerprint）只能有 1 行：输家回查既有行
        （created=False）并复用（replay 语义）。**无 Python 进程锁**；不允许
        update API（Outline / DraftSection / schema / payload 任一变化 =
        新指纹 = 新行）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(report, column.key)
            for column in ReportModel.__table__.columns
            if column.key not in excluded
        }
        stmt = insert(ReportModel).values(**values).on_conflict_do_nothing().returning(ReportModel)
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_fingerprint(report.report_fingerprint)
        if existing is None:
            raise RuntimeError("report conflict without existing row")
        return existing, False


class ReportCheckResultRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, check_result_id: UUID) -> ReportCheckResultModel | None:
        result = await self._session.execute(
            select(ReportCheckResultModel).where(
                ReportCheckResultModel.check_result_id == check_result_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_check_fingerprint(
        self, check_fingerprint: str
    ) -> ReportCheckResultModel | None:
        result = await self._session.execute(
            select(ReportCheckResultModel).where(
                ReportCheckResultModel.check_fingerprint == check_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, result: ReportCheckResultModel
    ) -> tuple[ReportCheckResultModel, bool]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING（无目标索引）。

        check_fingerprint = check schema + report_id + report_fingerprint +
        normalized findings → 同 report + 同 findings → replay 同一行；Report 内容
        变化 → report_fingerprint 不同 → 新指纹 → 新行（旧行保留，无 update API）。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(result, column.key)
            for column in ReportCheckResultModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ReportCheckResultModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ReportCheckResultModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_check_fingerprint(result.check_fingerprint)
        if existing is None:
            raise RuntimeError("report check result conflict without existing row")
        return existing, False
