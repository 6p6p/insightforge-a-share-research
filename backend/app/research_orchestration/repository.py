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
from app.research_orchestration.contracts import (
    ACTIVE_ORCHESTRATION_STATUSES,
    OrchestrationPhase,
)


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
        """按 fingerprint 查询（**非唯一**：user retry 后同 fingerprint 可多行）。

        只用于确认"存在任意一次该输入的尝试"；**replay / retry 定位必须用
        `get_by_plan_and_attempt` / `get_latest_for_plan`**（spec D/B 精确边界）。
        """
        result = await self._session.execute(
            select(ResearchOrchestrationModel).where(
                ResearchOrchestrationModel.input_fingerprint == fingerprint
            )
        )
        return result.scalars().first()

    async def get_by_plan_and_attempt(
        self, research_plan_id: UUID, attempt_no: int
    ) -> ResearchOrchestrationModel | None:
        """精确 replay / retry 定位：同 research_plan + attempt 至多一个
        orchestration（`uq_research_orchestration_runs_plan_attempt`）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationModel).where(
                ResearchOrchestrationModel.research_plan_id == research_plan_id,
                ResearchOrchestrationModel.attempt_no == attempt_no,
            )
        )
        return result.scalar_one_or_none()

    async def get_latest_for_plan(
        self, research_plan_id: UUID
    ) -> ResearchOrchestrationModel | None:
        """同 plan 最大 attempt 的 orchestration（retry 计算 new attempt 用）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationModel)
            .where(ResearchOrchestrationModel.research_plan_id == research_plan_id)
            .order_by(ResearchOrchestrationModel.attempt_no.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get_by_id_for_update(
        self, orchestration_id: UUID
    ) -> ResearchOrchestrationModel | None:
        """`SELECT ... FOR UPDATE`：串行化并发 retry（同 old → 最终只有一个新 attempt）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationModel)
            .where(ResearchOrchestrationModel.orchestration_id == orchestration_id)
            .with_for_update()
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

    async def get_latest_for_task(self, task_id: UUID) -> ResearchOrchestrationModel | None:
        """同 task 最近一条 orchestration（含 terminal history，`current` 投影用）。"""
        result = await self._session.execute(
            select(ResearchOrchestrationModel)
            .where(ResearchOrchestrationModel.task_id == task_id)
            .order_by(ResearchOrchestrationModel.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def create_or_get(
        self, orchestration: ResearchOrchestrationModel
    ) -> tuple[ResearchOrchestrationModel, bool]:
        """INSERT ... ON CONFLICT(research_plan_id, attempt_no) DO NOTHING RETURNING。

        同 plan 同 attempt → replay 已有行（并发最终只 1 行）。**input_fingerprint
        不再 UNIQUE**（7A.2B.2 spec B：user retry 同 fingerprint 多行并存）；
        唯一性由 `uq_research_orchestration_runs_plan_attempt` 承担。task_id 的
        active partial unique 冲突**不在** ON CONFLICT arbiter 上 → IntegrityError
        抛给调用方（service 捕获 → 409 或重查）。
        """
        if orchestration.research_plan_id is None:
            raise ValueError("research orchestration requires a research plan id")
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
            .on_conflict_do_nothing(
                index_elements=[
                    ResearchOrchestrationModel.research_plan_id,
                    ResearchOrchestrationModel.attempt_no,
                ]
            )
            .returning(ResearchOrchestrationModel)
        )
        result = await self._session.execute(stmt)
        row = result.scalar_one_or_none()
        if row is not None:
            return row, True
        existing = await self.get_by_plan_and_attempt(
            orchestration.research_plan_id, orchestration.attempt_no
        )
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
            orchestration_id,
            "completed",
            completed_at,
            error_code=None,
            error_message=None,
            current_phase=OrchestrationPhase.COMPLETED.value,
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
        current_phase: str | None = None,
    ) -> ResearchOrchestrationModel | None:
        values: dict = {
            "status": status,
            "completed_at": completed_at,
            "error_code": error_code,
            "error_message": error_message,
            "updated_at": datetime.now(UTC),
        }
        if current_phase is not None:
            values["current_phase"] = current_phase
        stmt = (
            update(ResearchOrchestrationModel)
            .where(ResearchOrchestrationModel.orchestration_id == orchestration_id)
            .values(**values)
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
