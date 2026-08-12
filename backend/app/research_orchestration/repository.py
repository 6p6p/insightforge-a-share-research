"""Research orchestration data access (stage 7A.2B.1): orchestration + child links.

事务由调用方（ResearchOrchestrationService / graph nodes / runner）协调；本模块
只做短查询与 create_or_get（ON CONFLICT replay 语义，无 Python 进程锁）。

- `ResearchOrchestrationRepository`：`research_orchestration_runs`（PK / input
  fingerprint UNIQUE / task_id active partial unique）；
- `ResearchOrchestrationChildRepository`：`research_orchestration_child_runs`
  —— **exact ownership lookup**（spec D）：`get_child(orchestration_id, stage,
  attempt_no)` 是唯一 allowed 的 child 定位方式，不得用
  `latest task + graph_name` 猜归属。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.research_orchestration import (
    ResearchOrchestrationChildModel,
    ResearchOrchestrationModel,
)
from app.research_orchestration.contracts import ACTIVE_ORCHESTRATION_STATUSES


class ResearchOrchestrationRepository:
    """research_orchestration_runs 访问（orchestration_id PK / input fp UNIQUE）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_id(self, orchestration_id: UUID) -> ResearchOrchestrationModel | None:
        result = await self._session.execute(
            select(ResearchOrchestrationModel).where(
                ResearchOrchestrationModel.orchestration_id == orchestration_id
            )
        )
        return result.scalar_one_or_none()

    async def get_by_input_fingerprint(self, fingerprint: str) -> ResearchOrchestrationModel | None:
        result = await self._session.execute(
            select(ResearchOrchestrationModel).where(
                ResearchOrchestrationModel.input_fingerprint == fingerprint
            )
        )
        return result.scalar_one_or_none()

    async def get_active_for_task(self, task_id: UUID) -> ResearchOrchestrationModel | None:
        """同 task 至多一个 active orchestration（partial unique index 语义）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationModel).where(
                ResearchOrchestrationModel.task_id == task_id,
                ResearchOrchestrationModel.status.in_(sorted(ACTIVE_ORCHESTRATION_STATUSES)),
            )
        )
        return result.scalars().first()

    async def create_or_get(
        self, orchestration: ResearchOrchestrationModel
    ) -> tuple[ResearchOrchestrationModel, bool]:
        """INSERT ... ON CONFLICT(input_fingerprint) DO NOTHING RETURNING。

        同 input（相同 input fingerprint）→ replay 同一行；并发最终只 1 行。
        task_id 的 active partial unique 冲突**不在** ON CONFLICT arbiter 上 →
        IntegrityError 抛给调用方（service 捕获 → 409 或重查）。
        """
        # orchestration_id 的 default=uuid.uuid4 是 Python-side：逐列取值会显式传
        # None 绕过默认 → 排除 PK，让 Core INSERT 应用列默认。created_at /
        # updated_at 有 server_default now()，同样排除。
        excluded = {"created_at", "updated_at", "orchestration_id"}
        values = {
            column.key: getattr(orchestration, column.key)
            for column in ResearchOrchestrationModel.__table__.columns
            if column.key not in excluded
        }
        stmt = (
            insert(ResearchOrchestrationModel)
            .values(**values)
            .on_conflict_do_nothing(index_elements=[ResearchOrchestrationModel.input_fingerprint])
            .returning(ResearchOrchestrationModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_input_fingerprint(orchestration.input_fingerprint)
        if existing is None:
            raise RuntimeError("research orchestration conflict without existing row")
        return existing, False

    async def update_progress(
        self,
        orchestration_id: UUID,
        *,
        status: str,
        current_phase: str,
    ) -> ResearchOrchestrationModel | None:
        """持久化 status + current_phase 推进（graph 节点用）。"""
        stmt = (
            update(ResearchOrchestrationModel)
            .where(ResearchOrchestrationModel.orchestration_id == orchestration_id)
            .values(
                status=status,
                current_phase=current_phase,
                updated_at=datetime.now(UTC),
            )
            .returning(ResearchOrchestrationModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def mark_completed(
        self, orchestration_id: UUID, completed_at: datetime
    ) -> ResearchOrchestrationModel | None:
        return await self._set_terminal(
            orchestration_id, "completed", completed_at, error_code=None, error_message=None
        )

    async def mark_cancelled(
        self, orchestration_id: UUID, completed_at: datetime
    ) -> ResearchOrchestrationModel | None:
        return await self._set_terminal(
            orchestration_id, "cancelled", completed_at, error_code=None, error_message=None
        )

    async def mark_failed(
        self,
        orchestration_id: UUID,
        completed_at: datetime,
        *,
        error_code: str,
        error_message: str | None = None,
    ) -> ResearchOrchestrationModel | None:
        return await self._set_terminal(
            orchestration_id,
            "failed",
            completed_at,
            error_code=error_code,
            error_message=error_message,
        )

    async def _set_terminal(
        self,
        orchestration_id: UUID,
        status: str,
        completed_at: datetime,
        *,
        error_code: str | None,
        error_message: str | None,
    ) -> ResearchOrchestrationModel | None:
        stmt = (
            update(ResearchOrchestrationModel)
            .where(ResearchOrchestrationModel.orchestration_id == orchestration_id)
            .values(
                status=status,
                completed_at=completed_at,
                error_code=error_code,
                error_message=error_message,
                updated_at=datetime.now(UTC),
            )
            .returning(ResearchOrchestrationModel)
        )
        result = await self._session.execute(stmt)
        return result.scalar_one_or_none()

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(ResearchOrchestrationModel)
        )
        return int(result.scalar_one())


class ResearchOrchestrationChildRepository:
    """research_orchestration_child_runs 访问（exact ownership linkage, spec D）。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_child(
        self, orchestration_id: UUID, stage: str, attempt_no: int
    ) -> ResearchOrchestrationChildModel | None:
        """exact child lookup（spec D 最重要 correctness boundary）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationChildModel).where(
                ResearchOrchestrationChildModel.orchestration_id == orchestration_id,
                ResearchOrchestrationChildModel.stage == stage,
                ResearchOrchestrationChildModel.attempt_no == attempt_no,
            )
        )
        return result.scalar_one_or_none()

    async def get_by_run_id(self, workflow_run_id: UUID) -> ResearchOrchestrationChildModel | None:
        """一个 WorkflowRun 至多被一个 orchestration 拥有（UNIQUE(workflow_run_id)）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationChildModel).where(
                ResearchOrchestrationChildModel.workflow_run_id == workflow_run_id
            )
        )
        return result.scalar_one_or_none()

    async def list_children(self, orchestration_id: UUID) -> list[ResearchOrchestrationChildModel]:
        result = await self._session.execute(
            select(ResearchOrchestrationChildModel)
            .where(ResearchOrchestrationChildModel.orchestration_id == orchestration_id)
            .order_by(ResearchOrchestrationChildModel.created_at.asc())
        )
        return list(result.scalars().all())

    async def attach_child(self, child: ResearchOrchestrationChildModel) -> None:
        """INSERT child link（UNIQUE 冲突 → IntegrityError 交给调用方 / runner）。"""
        self._session.add(child)

    async def count(self) -> int:
        result = await self._session.execute(
            select(func.count()).select_from(ResearchOrchestrationChildModel)
        )
        return int(result.scalar_one())
