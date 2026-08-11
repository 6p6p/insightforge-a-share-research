"""Data access for evidence-bound report audits (stage 5D).

`report_audits` 按 `audit_input_fingerprint` UNIQUE 做 **create_or_get**
（ON CONFLICT DO NOTHING 无目标索引，镜像 `ReportCheckResultRepository`）；
`review_issues` 按 `(audit_id, ordinal)` UNIQUE，只随所属 audit 一起写入 /
读取（**不建 link table**，spec G）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.report_audit import ReportAuditModel, ReviewIssueModel


class ReportAuditRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, audit_id: UUID) -> ReportAuditModel | None:
        result = await self._session.execute(
            select(ReportAuditModel).where(ReportAuditModel.audit_id == audit_id)
        )
        return result.scalar_one_or_none()

    async def get_by_audit_input_fingerprint(
        self, audit_input_fingerprint: str
    ) -> ReportAuditModel | None:
        result = await self._session.execute(
            select(ReportAuditModel).where(
                ReportAuditModel.audit_input_fingerprint == audit_input_fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def create_or_get(self, audit: ReportAuditModel) -> tuple[ReportAuditModel, bool]:
        """INSERT ... ON CONFLICT DO NOTHING RETURNING（无目标索引）。

        并发下相同 input（同一 audit_input_fingerprint）只能有 1 行：输家回查
        既有行（created=False）并复用（replay 语义）。**无 Python 进程锁**；
        不允许 update API（report / check / pack / schema / auditor 任一变化 =
        新输入指纹 = 新行）。audit_fingerprint **不 UNIQUE**，唯一性由 input
        指纹保证。
        """
        excluded = {"created_at"}
        values = {
            column.key: getattr(audit, column.key)
            for column in ReportAuditModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ReportAuditModel)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(ReportAuditModel)
        )
        inserted = await self._session.execute(stmt)
        row = inserted.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_audit_input_fingerprint(audit.audit_input_fingerprint)
        if existing is None:
            raise RuntimeError("report audit conflict without existing row")
        return existing, False

    async def list_issues(self, audit_id: UUID) -> list[ReviewIssueModel]:
        """按 ordinal（deterministic spec R 顺序）加载一次审计的全部 ReviewIssue。"""
        result = await self._session.execute(
            select(ReviewIssueModel)
            .where(ReviewIssueModel.audit_id == audit_id)
            .order_by(ReviewIssueModel.ordinal)
        )
        return list(result.scalars().all())
