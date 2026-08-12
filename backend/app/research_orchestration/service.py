"""Research orchestration service (stage 7A.2B.1 spec F/G/K/Q).

- **create_or_get_orchestration(task_id)**：经 `ResearchPlanningService.create_plan`
  内部 create/replay ResearchPlan v2（0 次额外 LLM，同输入 replay）→ 取 planner
  input fingerprint → 计算 orchestration input fingerprint（spec F）→ replay /
  create。并发 create → 最终 1 orchestration；
- **verify_orchestration_integrity(orchestration_id)**：重放 stored plan 的
  planner input fingerprint 重建 orchestration fingerprint，与行交叉核对
  （task_id / research_plan_id / planner input）。**不重新调用 planner / LLM**；
- **cancel_orchestration(orchestration_id)**：minimal cancel（spec Q）——先按现有
  Stage4/WorkflowRun cancel 语义（`WorkflowRunRepository.mark_cancelled`，即
  WorkflowExecutionManager.cancel_run 的 DB 层入口）取消 active child，再
  orchestration status=cancelled；幂等；**不直接 SQL 删除 child / orchestration**
  （不产生孤儿 WorkflowRun）。

user retry 语义（spec P）：input_fingerprint 对 (task, plan input, orchestrator)
确定性。task 输入不变 → create_or_get replay 同一 orchestration（**不是复活**）；
真正的新尝试需要 task 输入变化 → 新 fingerprint → 新 orchestration。新 fingerprint
的 create 在 task 已有 active orchestration 时 → `ResearchOrchestrationActiveConflict`
（409）。旧 orchestration / Plan / ChildRun / WorkflowRun / Checkpoint 全部保留。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import ActiveWorkflowRunExists
from app.db.models.research_orchestration import (
    ResearchOrchestrationChildModel,
    ResearchOrchestrationModel,
)
from app.domain.tasks import ACTIVE_WORKFLOW_RUN_STATUSES
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
    ChildStage,
    OrchestrationPhase,
    OrchestrationStatus,
    compute_orchestration_input_fingerprint,
)
from app.research_orchestration.errors import (
    ResearchOrchestrationActiveConflict,
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationIntegrityError,
    ResearchOrchestrationNotFound,
)
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from app.research_planning.repository import ResearchPlanRepository
from app.research_planning.service import ResearchPlanningService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.runner import Stage4WorkflowRunner

_ACTIVE_RUN_VALUES = {status.value for status in ACTIVE_WORKFLOW_RUN_STATUSES}


@dataclass(frozen=True)
class ResearchOrchestrationResult:
    """一次 top-level orchestration 的只读摘要（不含 plan / child 正文）。"""

    orchestration_id: UUID
    task_id: UUID
    research_plan_id: UUID | None
    orchestration_schema_version: int
    orchestrator_name: str
    orchestrator_version: int
    status: str
    current_phase: str
    input_fingerprint: str
    started_at: datetime | None
    completed_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    replayed: bool = False


class ResearchOrchestrationService:
    """Top-level research orchestration 应用服务（create / read / verify / cancel）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        plan_service: ResearchPlanningService,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._plan_service = plan_service

    # ------------------------------------------------------------------ create

    async def create_or_get_orchestration(self, task_id: UUID) -> ResearchOrchestrationResult:
        """task → create/replay ResearchPlan v2 → orchestration fingerprint → replay / create。

        **并发 create → 最终 1 orchestration**：replay 由 input_fingerprint UNIQUE
        保证；新 fingerprint 的并发由 task_id active partial unique 兜底
        （IntegrityError → 重查：fingerprint 命中 → 返回已有行；否则 409）。
        """
        plan_result = await self._plan_service.create_plan(task_id)
        fingerprint = compute_orchestration_input_fingerprint(
            orchestration_schema_version=ORCHESTRATION_SCHEMA_VERSION,
            task_id=task_id,
            planner_input_fingerprint=plan_result.planner_input_fingerprint,
            orchestrator_name=ORCHESTRATOR_NAME,
            orchestrator_version=ORCHESTRATOR_VERSION,
        )
        async with self._sessionmaker() as session:
            repo = ResearchOrchestrationRepository(session)
            existing = await repo.get_by_input_fingerprint(fingerprint)
            if existing is not None:
                return self._to_result(existing, replayed=True)

            active = await repo.get_active_for_task(task_id)
            if active is not None:
                raise ResearchOrchestrationActiveConflict()

            orchestration = ResearchOrchestrationModel(
                task_id=task_id,
                research_plan_id=plan_result.research_plan_id,
                orchestration_schema_version=ORCHESTRATION_SCHEMA_VERSION,
                orchestrator_name=ORCHESTRATOR_NAME,
                orchestrator_version=ORCHESTRATOR_VERSION,
                status=OrchestrationStatus.PENDING.value,
                current_phase=OrchestrationPhase.PLANNING.value,
                input_fingerprint=fingerprint,
                started_at=datetime.now(UTC),
            )
            try:
                row, created = await repo.create_or_get(orchestration)
            except IntegrityError:
                # 并发：可能指纹冲突（replay 胜出）或 task_id active 冲突。
                await session.rollback()
                winner = await repo.get_by_input_fingerprint(fingerprint)
                if winner is not None:
                    return self._to_result(winner, replayed=True)
                active = await repo.get_active_for_task(task_id)
                if active is not None:
                    raise ResearchOrchestrationActiveConflict() from None
                raise
            await session.commit()
            return self._to_result(row, replayed=not created)

    # ------------------------------------------------------------------ read

    async def get_orchestration(self, orchestration_id: UUID) -> ResearchOrchestrationResult:
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        return self._to_result(orchestration)

    # ------------------------------------------------------------------ verify

    async def verify_orchestration_integrity(self, orchestration_id: UUID):
        """重放 stored plan 的 planner input fingerprint 重建 orchestration fingerprint。

        与 orchestration 行交叉核对（task_id / research_plan_id / planner input），
        不一致 → `ResearchOrchestrationIntegrityError`。**不重新调用 planner / LLM**。
        """
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
            if orchestration is None:
                raise ResearchOrchestrationNotFound()
            if orchestration.research_plan_id is None:
                raise ResearchOrchestrationIntegrityError(
                    "research orchestration research plan missing"
                )
            plan = await ResearchPlanRepository(session).get_by_id(orchestration.research_plan_id)
            stored_task_id = orchestration.task_id
            stored_fp = orchestration.input_fingerprint
            stored_schema = orchestration.orchestration_schema_version
            stored_name = orchestration.orchestrator_name
            stored_version = orchestration.orchestrator_version

        if plan is None:
            raise ResearchOrchestrationIntegrityError("research orchestration plan missing")
        if plan.task_id != stored_task_id:
            raise ResearchOrchestrationIntegrityError(
                "research orchestration task identity mismatch (plan task tampered)"
            )

        recomputed = compute_orchestration_input_fingerprint(
            orchestration_schema_version=stored_schema,
            task_id=stored_task_id,
            planner_input_fingerprint=plan.planner_input_fingerprint,
            orchestrator_name=stored_name,
            orchestrator_version=stored_version,
        )
        if recomputed != stored_fp:
            raise ResearchOrchestrationIntegrityError(
                "research orchestration fingerprint mismatch (input tampered)"
            )
        return orchestration

    # ------------------------------------------------------------------ cancel

    async def cancel_orchestration(self, orchestration_id: UUID) -> ResearchOrchestrationResult:
        """minimal cancel（spec Q）：先取消 active child，再 orchestration cancelled。

        幂等：已 cancelled → 原样返回；已 completed/failed →
        `ResearchOrchestrationAlreadyFinished`。child 取消复用现有
        Stage4/WorkflowRun cancel 的 DB 层入口（`mark_cancelled`）；**不直接 SQL
        删除 child / orchestration**。
        """
        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            repo = ResearchOrchestrationRepository(session)
            orchestration = await repo.get_by_id(orchestration_id)
            if orchestration is None:
                raise ResearchOrchestrationNotFound()
            if orchestration.status == OrchestrationStatus.CANCELLED.value:
                return self._to_result(orchestration)
            if orchestration.status in (
                OrchestrationStatus.COMPLETED.value,
                OrchestrationStatus.FAILED.value,
            ):
                raise ResearchOrchestrationAlreadyFinished()

            child_repo = ResearchOrchestrationChildRepository(session)
            run_repo = WorkflowRunRepository(session)
            for child in await child_repo.list_children(orchestration_id):
                run = await run_repo.get_by_id(child.workflow_run_id)
                if run is not None and run.status in _ACTIVE_RUN_VALUES:
                    await run_repo.mark_cancelled(child.workflow_run_id, now)

            orchestration = await repo.mark_cancelled(orchestration_id, now)
            await session.commit()
            return self._to_result(orchestration)

    # ------------------------------------------------------------------ internal

    @staticmethod
    def _to_result(
        orchestration: ResearchOrchestrationModel, *, replayed: bool = False
    ) -> ResearchOrchestrationResult:
        return ResearchOrchestrationResult(
            orchestration_id=orchestration.orchestration_id,
            task_id=orchestration.task_id,
            research_plan_id=orchestration.research_plan_id,
            orchestration_schema_version=orchestration.orchestration_schema_version,
            orchestrator_name=orchestration.orchestrator_name,
            orchestrator_version=orchestration.orchestrator_version,
            status=orchestration.status,
            current_phase=orchestration.current_phase,
            input_fingerprint=orchestration.input_fingerprint,
            started_at=(
                orchestration.started_at.astimezone(UTC)
                if orchestration.started_at is not None
                else None
            ),
            completed_at=(
                orchestration.completed_at.astimezone(UTC)
                if orchestration.completed_at is not None
                else None
            ),
            error_code=orchestration.error_code,
            error_message=orchestration.error_message,
            created_at=orchestration.created_at.astimezone(UTC),
            replayed=replayed,
        )


@dataclass(frozen=True)
class ChildStage4Result:
    """orchestration → Stage4 child 的定位结果（exact run，不含 request 正文）。"""

    run_id: UUID
    created: bool


class ResearchOrchestrationChildService:
    """orchestration → Stage4 child 的 exact ownership（spec C/D/K）。

    **ensure_stage4_child**：exact child `(orchestration_id, stage4, attempt 1)`
    ——**绝不用 `latest task + graph_name` 猜归属**。

    - 已存在 → 返回 exact run（recovery / re-run 复用，不重复 create）；
    - 不存在 → `Stage4WorkflowRunner.create_stage4_run(child_bind=...)` **同一事务**
      创建 WorkflowRun + child link（spec K same-transaction：run create 与 link
      insert 之间无 crash 孤儿窗口）；
    - 并发（active index / child link UNIQUE 冲突）→ `ActiveWorkflowRunExists`
      → 重查 `get_child` → 命中返回 winner，否则原样抛。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        runner: Stage4WorkflowRunner,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runner = runner

    async def ensure_stage4_child(
        self,
        orchestration_id: UUID,
        stage4_request: Stage4WorkflowRequest,
    ) -> ChildStage4Result:
        async with self._sessionmaker() as session:
            child_repo = ResearchOrchestrationChildRepository(session)
            existing = await child_repo.get_child(orchestration_id, ChildStage.STAGE4.value, 1)
            if existing is not None:
                run = await WorkflowRunRepository(session).get_by_id(existing.workflow_run_id)
                if run is None:
                    raise ResearchOrchestrationIntegrityError("orchestration child run missing")
                return ChildStage4Result(run_id=run.run_id, created=False)

        def _bind(run_id: UUID) -> ResearchOrchestrationChildModel:
            return ResearchOrchestrationChildModel(
                orchestration_id=orchestration_id,
                workflow_run_id=run_id,
                stage=ChildStage.STAGE4.value,
                attempt_no=1,
            )

        try:
            result = await self._runner.create_stage4_run(stage4_request, child_bind=_bind)
        except ActiveWorkflowRunExists:
            # 并发：另一个 active WorkflowRun（可能是本 orchestration 的 stage4
            # child）已存在 → exact 重查，命中返回 winner。
            async with self._sessionmaker() as session:
                existing = await ResearchOrchestrationChildRepository(session).get_child(
                    orchestration_id, ChildStage.STAGE4.value, 1
                )
            if existing is not None:
                return ChildStage4Result(run_id=existing.workflow_run_id, created=False)
            raise
        return ChildStage4Result(run_id=result.run_id, created=True)
