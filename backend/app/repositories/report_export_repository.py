"""Report export persistence repository (stage 6C).

并发安全 create-or-get：`export_input_fingerprint` UNIQUE 是唯一性来源——
`INSERT ... ON CONFLICT DO NOTHING RETURNING`；并发 loser 拿不到 returned 行时
回查指纹命中 winner 行（**不产生第二行**，spec M）。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.report_export import ReportExportModel


class ReportExportRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, export_id: UUID) -> ReportExportModel | None:
        return (
            await self._session.execute(
                select(ReportExportModel).where(ReportExportModel.export_id == export_id)
            )
        ).scalar_one_or_none()

    async def get_by_fingerprint(self, fingerprint: str) -> ReportExportModel | None:
        return (
            await self._session.execute(
                select(ReportExportModel).where(
                    ReportExportModel.export_input_fingerprint == fingerprint
                )
            )
        ).scalar_one_or_none()

    async def create_or_get(
        self,
        *,
        export_id: UUID,
        task_id: UUID,
        report_id: UUID,
        check_result_id: UUID,
        audit_id: UUID,
        human_decision_id: UUID | None,
        export_schema_version: int,
        export_format: str,
        export_input_fingerprint: str,
        content_sha256: str,
        byte_size: int,
        media_type: str,
        file_name: str,
        storage_key: str,
    ) -> ReportExportModel:
        """INSERT ... ON CONFLICT DO NOTHING；conflict → 回查 winner 行。"""
        stmt = (
            insert(ReportExportModel)
            .values(
                export_id=export_id,
                task_id=task_id,
                report_id=report_id,
                check_result_id=check_result_id,
                audit_id=audit_id,
                human_decision_id=human_decision_id,
                export_schema_version=export_schema_version,
                export_format=export_format,
                export_input_fingerprint=export_input_fingerprint,
                content_sha256=content_sha256,
                byte_size=byte_size,
                media_type=media_type,
                file_name=file_name,
                storage_key=storage_key,
            )
            .on_conflict_do_nothing(index_elements=[ReportExportModel.export_input_fingerprint])
            .returning(ReportExportModel)
        )
        row = (await self._session.execute(stmt)).scalar_one_or_none()
        if row is not None:
            return row
        winner = await self.get_by_fingerprint(export_input_fingerprint)
        if winner is None:
            # 不应发生：conflict 但回查为空 → 视为完整性失败（不静默创建重复行）。
            raise LookupError("report_export conflict but no existing row by fingerprint")
        return winner
