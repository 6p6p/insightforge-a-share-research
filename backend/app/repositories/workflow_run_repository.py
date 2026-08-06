"""Data access for workflow runs."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import (
    ACTIVE_WORKFLOW_RUN_STATUSES,
    ORPHANED_WORKFLOW_RUN_STATUSES,
    WorkflowRunStatus,
)


class WorkflowRunRepository:
    """Repository scoped to a single AsyncSession; transactions are coordinated by callers."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @property
    def session(self) -> AsyncSession:
        return self._session

    async def create(self, run: WorkflowRunModel) -> WorkflowRunModel:
        self._session.add(run)
        await self._session.flush()
        return run

    async def get_by_id(self, run_id: UUID) -> WorkflowRunModel | None:
        result = await self._session.execute(
            select(WorkflowRunModel).where(WorkflowRunModel.run_id == run_id)
        )
        return result.scalar_one_or_none()

    async def get_by_thread_id(self, thread_id: str) -> WorkflowRunModel | None:
        result = await self._session.execute(
            select(WorkflowRunModel).where(WorkflowRunModel.thread_id == thread_id)
        )
        return result.scalar_one_or_none()

    async def get_active_for_task(self, task_id: UUID) -> WorkflowRunModel | None:
        result = await self._session.execute(
            select(WorkflowRunModel).where(
                WorkflowRunModel.task_id == task_id,
                WorkflowRunModel.status.in_(
                    [status.value for status in ACTIVE_WORKFLOW_RUN_STATUSES]
                ),
            )
        )
        return result.scalars().first()

    async def claim_pending(
        self,
        run_id: UUID,
        started_at: datetime,
    ) -> WorkflowRunModel | None:
        """Atomically claim a pending run; returns None if missing or not pending."""
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.run_id == run_id,
                WorkflowRunModel.status == WorkflowRunStatus.PENDING.value,
            )
            .values(
                status=WorkflowRunStatus.RUNNING.value,
                started_at=started_at,
                updated_at=datetime.now(UTC),
            )
            .returning(WorkflowRunModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def claim_waiting_human(
        self,
        run_id: UUID,
        started_at: datetime,
    ) -> WorkflowRunModel | None:
        """Atomically claim a waiting_human run; returns None if missing or not waiting_human."""
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.run_id == run_id,
                WorkflowRunModel.status == WorkflowRunStatus.WAITING_HUMAN.value,
            )
            .values(
                status=WorkflowRunStatus.RUNNING.value,
                pending_action=None,
                started_at=started_at,
                updated_at=datetime.now(UTC),
            )
            .returning(WorkflowRunModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_waiting_human(
        self,
        run_id: UUID,
        pending_action: str,
    ) -> WorkflowRunModel | None:
        """Atomically move a running run to waiting_human; returns None otherwise."""
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.run_id == run_id,
                WorkflowRunModel.status == WorkflowRunStatus.RUNNING.value,
            )
            .values(
                status=WorkflowRunStatus.WAITING_HUMAN.value,
                pending_action=pending_action,
                updated_at=datetime.now(UTC),
            )
            .returning(WorkflowRunModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_cancelled(
        self,
        run_id: UUID,
        cancelled_at: datetime,
    ) -> WorkflowRunModel | None:
        """Atomically cancel a pending/running/waiting_human run; returns None if terminal."""
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.run_id == run_id,
                WorkflowRunModel.status.in_(
                    [status.value for status in ACTIVE_WORKFLOW_RUN_STATUSES]
                ),
            )
            .values(
                status=WorkflowRunStatus.CANCELLED.value,
                pending_action=None,
                updated_at=datetime.now(UTC),
            )
            .returning(WorkflowRunModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_orphaned_failed(
        self,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> list[WorkflowRunModel]:
        """Atomically fail every orphaned pending/running run; returns the updated runs."""
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.status.in_(
                    [status.value for status in ORPHANED_WORKFLOW_RUN_STATUSES]
                )
            )
            .values(
                status=WorkflowRunStatus.FAILED.value,
                failed_at=failed_at,
                error_code=error_code,
                error_message=error_message,
                updated_at=datetime.now(UTC),
            )
            .returning(WorkflowRunModel)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def mark_running(
        self,
        run_id: UUID,
        started_at: datetime,
    ) -> WorkflowRunModel | None:
        run = await self.get_by_id(run_id)
        if run is None:
            return None
        run.status = WorkflowRunStatus.RUNNING.value
        run.started_at = started_at
        return run

    async def mark_completed(
        self,
        run_id: UUID,
        completed_at: datetime,
    ) -> WorkflowRunModel | None:
        run = await self.get_by_id(run_id)
        if run is None:
            return None
        run.status = WorkflowRunStatus.COMPLETED.value
        run.completed_at = completed_at
        return run

    async def mark_failed(
        self,
        run_id: UUID,
        failed_at: datetime,
        error_code: str,
        error_message: str,
    ) -> WorkflowRunModel | None:
        run = await self.get_by_id(run_id)
        if run is None:
            return None
        run.status = WorkflowRunStatus.FAILED.value
        run.failed_at = failed_at
        run.error_code = error_code
        run.error_message = error_message
        return run

    async def list_for_task(
        self,
        task_id: UUID,
        limit: int,
        offset: int,
    ) -> tuple[list[WorkflowRunModel], int]:
        base = WorkflowRunModel.task_id == task_id
        count_query = select(func.count()).select_from(WorkflowRunModel).where(base)
        total = (await self._session.execute(count_query)).scalar_one()
        query = (
            select(WorkflowRunModel)
            .where(base)
            .order_by(
                WorkflowRunModel.created_at.desc(),
                WorkflowRunModel.run_id.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        rows = (await self._session.execute(query)).scalars().all()
        return list(rows), total
