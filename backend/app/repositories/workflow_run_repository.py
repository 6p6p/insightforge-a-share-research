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

    async def claim_failed_for_recovery(
        self,
        run_id: UUID,
        started_at: datetime,
    ) -> WorkflowRunModel | None:
        """Atomically reclaim a failed run for durable recovery; returns None if not failed.

        Stage 4 durable recovery（spec N-O）：run 失败后，新 runner + 同
        run_id / thread_id 从最后 checkpoint 继续。仅允许 FAILED → RUNNING。
        **不是**用户 retry：Stage 1 用户 retry 走 create_simulation_run → 新
        run / 新 thread；本方法只服务 Stage 4 内部 recovery，复用同 run/thread。
        恢复后清空 terminal failure 字段（error_code / error_message /
        completed_at），不得残留失败的元数据（Gate0-B 共享修复）。
        """
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.run_id == run_id,
                WorkflowRunModel.status == WorkflowRunStatus.FAILED.value,
            )
            .values(
                status=WorkflowRunStatus.RUNNING.value,
                started_at=started_at,
                error_code=None,
                error_message=None,
                completed_at=None,
                updated_at=datetime.now(UTC),
            )
            .returning(WorkflowRunModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def claim_failed_for_recovery_gated(
        self,
        run_id: UUID,
        started_at: datetime,
        *,
        graph_name: str,
        error_code: str,
    ) -> WorkflowRunModel | None:
        """Atomically reclaim a failed run for worker-restart recovery, gated.

        仅允许 `graph_name` + `status=failed` + `error_code`（worker_restarted）
        → RUNNING，复用同 run_id / thread_id 从最后 checkpoint 继续。**不是**
        用户 retry；业务失败（LLM / 校验 / 终态错误）与 WAITING_HUMAN 一律不
        在该路径——WAITING_HUMAN 人工裁决走 `claim_waiting_human`。
        gate 常量由调用方注入（Stage 5 runner 传 STAGE5_GRAPH_NAME +
        WORKER_RESTARTED_ERROR_CODE），避免 repository 反向依赖 services/stage5。
        恢复后清空 terminal failure 字段（error_code / error_message /
        completed_at），不得残留失败的元数据（Gate0-B 共享修复，与 Stage4
        `claim_failed_for_recovery` 一致）。
        """
        stmt = (
            update(WorkflowRunModel)
            .where(
                WorkflowRunModel.run_id == run_id,
                WorkflowRunModel.graph_name == graph_name,
                WorkflowRunModel.status == WorkflowRunStatus.FAILED.value,
                WorkflowRunModel.error_code == error_code,
            )
            .values(
                status=WorkflowRunStatus.RUNNING.value,
                started_at=started_at,
                error_code=None,
                error_message=None,
                completed_at=None,
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

    async def get_latest_for_task(self, task_id: UUID) -> WorkflowRunModel | None:
        """最近创建的一次 run（workspace current_run 投影）。"""
        rows, _ = await self.list_for_task(task_id, limit=1, offset=0)
        return rows[0] if rows else None

    async def get_latest_for_task_by_graph(
        self,
        task_id: UUID,
        graph_name: str,
    ) -> WorkflowRunModel | None:
        """任务最近一条指定 graph 的 run（artifact workspace 的 Stage4/Stage5 锚定）。

        语义与 ResearchExecutionRecoveryCoordinator 一致：取该 task 最近一条
        Stage4 / Stage5 run 作为当前研究周期链尾。
        """
        stmt = (
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.task_id == task_id,
                WorkflowRunModel.graph_name == graph_name,
            )
            .order_by(
                WorkflowRunModel.created_at.desc(),
                WorkflowRunModel.run_id.desc(),
            )
            .limit(1)
        )
        result = await self._session.execute(stmt)
        return result.scalars().first()

    async def list_for_task_by_graph(
        self,
        task_id: UUID,
        graph_name: str,
        limit: int = 50,
    ) -> list[WorkflowRunModel]:
        """任务按 graph 倒序枚举 run（canonical lineage 的 Stage4 匹配用）。

        Stage 6B.1 spec B：canonical synthesis = 最新 Stage5 checkpoint 的
        `synthesis_result_id`；`matched_stage4_run` 从该 task 的全部 Stage4 run
        中选 checkpoint `.synthesis_result_id == canonical` 的那一条（research
        backflow 的新 Synthesis 没有对应 Stage4 run → 合法无匹配）。
        """
        stmt = (
            select(WorkflowRunModel)
            .where(
                WorkflowRunModel.task_id == task_id,
                WorkflowRunModel.graph_name == graph_name,
            )
            .order_by(
                WorkflowRunModel.created_at.desc(),
                WorkflowRunModel.run_id.desc(),
            )
            .limit(limit)
        )
        result = await self._session.execute(stmt)
        return list(result.scalars().all())
