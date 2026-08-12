"""Research orchestration service (stage 7A.2B.1 spec F/G/K/Q + 7A.2B.2 spec B/C/N/P).

- **create_or_get_orchestration(task_id)**：经 `ResearchPlanningService.create_plan`
  内部 create/replay ResearchPlan v2（0 次额外 LLM，同输入 replay）→ 取 planner
  input fingerprint → 计算 orchestration input fingerprint（spec F）→ 以
  `(research_plan_id, attempt_no=1)` 精确 replay / create。并发 create → 最终
  1 orchestration；
- **retry_orchestration(orchestration_id)**（7A.2B.2 spec C）：**真正 user retry**
  ——同 ResearchPlan、即使 research input 完全相同，也生成 **NEW
  orchestration_id + NEW top-level thread**。只允许 failed / cancelled（completed /
  active → reject）；verify old orchestration + ResearchPlan；`new_attempt =
  max attempt + 1`；same task_id / research_plan_id / input_fingerprint；
  `retry_of = old orchestration_id`；old 完全不改（attempt 1/2/3 并存历史）。
  并发 retry（同 old）→ FOR UPDATE 串行化 → 最终只有一个 attempt=2；
- **verify_orchestration_integrity(orchestration_id)**：重放 stored plan 的
  planner input fingerprint 重建 orchestration fingerprint，与行交叉核对
  （task_id / research_plan_id / planner input），并验证 retry_of 必须同
  task/plan（spec B service integrity）。**不重新调用 planner / LLM**；
- **cancel_orchestration(orchestration_id)**：minimal cancel（spec Q）——先按现有
  Stage4/WorkflowRun cancel 语义（`WorkflowRunRepository.mark_cancelled`，即
  WorkflowExecutionManager.cancel_run 的 DB 层入口）取消 active child，再
  orchestration status=cancelled；幂等；**不直接 SQL 删除 child / orchestration**
  （不产生孤儿 WorkflowRun）；
- **act_on_orchestration(orchestration_id, action, comment)**（7A.2B.2 spec
  N/O/P）：人工裁决 —— **仅 waiting_human**。`approve`/`rewrite`/`research` 先把
  immutable human decision 提交到 exact Stage5 child（`resume_stage5_human`），再
  `run_orchestration` 继续顶层（continuation → `route_stage5_result`：approve →
  complete / rewrite → 重新 awaiting_stage5 / research → pause_for_research 只
  持久化 research_request_id + phase=research_backflow，**不做 backflow 循环**，
  spec P）；`cancel` 委托 `cancel_orchestration`。runners 未绑定 → RuntimeError。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:  # pragma: no cover — 仅类型注解
    from app.research_orchestration.runner import ResearchOrchestrationRunner

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import ActiveWorkflowRunExists
from app.db.models.research_orchestration import (
    ResearchOrchestrationChildModel,
    ResearchOrchestrationModel,
)
from app.domain.tasks import ACTIVE_WORKFLOW_RUN_STATUSES
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    ACTIVE_ORCHESTRATION_STATUSES,
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
    ResearchOrchestrationChildConflict,
    ResearchOrchestrationChildNotFound,
    ResearchOrchestrationIntegrityError,
    ResearchOrchestrationInvalidAction,
    ResearchOrchestrationNotFound,
)
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from app.research_planning.repository import ResearchPlanRepository
from app.research_planning.service import ResearchPlanningService
from app.review.contracts import (
    HUMAN_DECISION_APPROVE,
    HUMAN_DECISION_CANCEL,
    HUMAN_DECISION_RESEARCH,
    HUMAN_DECISION_REWRITE,
)
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.contracts import Stage5WorkflowRequest
from app.stage5.runner import Stage5WorkflowRunner

_ACTIVE_RUN_VALUES = {status.value for status in ACTIVE_WORKFLOW_RUN_STATUSES}

# 首次尝试的 attempt_no（replay / create 定位到 attempt=1；user retry 从 2 起）。
_FIRST_ATTEMPT = 1

# `act_on_orchestration` 中会把 human decision 转发到 Stage5 child resume 的
# action（spec N/O/P）。cancel 单独委托 cancel_orchestration（含自身幂等规则）。
_HUMAN_RESUME_ACTIONS = frozenset(
    {HUMAN_DECISION_APPROVE, HUMAN_DECISION_REWRITE, HUMAN_DECISION_RESEARCH}
)

# Stage4/5 runner 的 child link 归属约束（spec E）：这些 IntegrityError 在
# orchestration 层分类为 `ResearchOrchestrationChildConflict`（409），不是
# active-run 冲突。
_CHILD_OWNERSHIP_CONSTRAINTS = {
    "uq_research_orchestration_child_runs_workflow_run_id",
    "uq_research_orchestration_child_runs_scope_attempt",
}


def _constraint_name(exc: IntegrityError) -> str | None:
    """从 PostgreSQL Diagnostics 取违反的约束名（无 diag / 非约束错误 → None）。"""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


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
    attempt_no: int
    retry_of_orchestration_id: UUID | None
    replayed: bool = False


class ResearchOrchestrationService:
    """Top-level research orchestration 应用服务（create / read / verify / cancel）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        plan_service: ResearchPlanningService,
        stage5_runner: Stage5WorkflowRunner | None = None,
        orchestration_runner: ResearchOrchestrationRunner | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._plan_service = plan_service
        # human action（spec N/P）：approve/rewrite/research 需要 Stage5 runner
        # resume child + 顶层 runner 继续 continuation；未绑定 → RuntimeError
        # （production factory 绑定，unit 测试可只测 dispatch 守卫）。
        self._stage5_runner = stage5_runner
        self._orchestration_runner = orchestration_runner

    @property
    def orchestration_runner(self) -> ResearchOrchestrationRunner | None:
        """只读：lifespan recovery coordinator / API 复用同一顶层 runner。

        production factory（`_create_research_orchestration`）绑定；unit 测试不绑
        → None（dispatch 守卫不触碰）。
        """
        return self._orchestration_runner

    # ------------------------------------------------------------------ create

    async def create_or_get_orchestration(self, task_id: UUID) -> ResearchOrchestrationResult:
        """task → create/replay ResearchPlan v2 → orchestration fingerprint → replay / create。

        **并发 create → 最终 1 orchestration**：replay 以 `(research_plan_id,
        attempt_no=1)` 精确定位（`uq_research_orchestration_runs_plan_attempt`）；
        新 fingerprint 的并发由 task_id active partial unique 兜底（IntegrityError
        → 重查 plan+attempt=1：命中返回已有行；否则 409）。**input_fingerprint 不再
        是唯一键**（7A.2B.2 spec B：user retry 同 fingerprint 多行并存），同输入
        replay 定位到 attempt=1 的首次尝试。
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
            existing = await repo.get_by_plan_and_attempt(
                plan_result.research_plan_id, _FIRST_ATTEMPT
            )
            if existing is not None:
                return self._to_result(existing, replayed=True)

            active = await repo.get_active_for_task(task_id)
            if active is not None:
                raise ResearchOrchestrationActiveConflict()

            orchestration = ResearchOrchestrationModel(
                task_id=task_id,
                research_plan_id=plan_result.research_plan_id,
                attempt_no=_FIRST_ATTEMPT,
                retry_of_orchestration_id=None,
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
                # 并发：可能 plan+attempt 冲突（replay 胜出，但 ON CONFLICT 已处理）
                # 或 task_id active 冲突（IntegrityError 来源于此）。
                await session.rollback()
                winner = await repo.get_by_plan_and_attempt(
                    plan_result.research_plan_id, _FIRST_ATTEMPT
                )
                if winner is not None:
                    return self._to_result(winner, replayed=True)
                active = await repo.get_active_for_task(task_id)
                if active is not None:
                    raise ResearchOrchestrationActiveConflict() from None
                raise
            await session.commit()
            return self._to_result(row, replayed=not created)

    # ------------------------------------------------------------------ start

    async def start_orchestration(self, task_id: UUID) -> ResearchOrchestrationResult:
        """create/replay → 未终态则顶层 run（spec U：默认自动研究入口）。

        - create 已有 orchestration 时：active（pending/running/waiting_human）
          → checkpoint-aware continuation（同 orchestration_id + 同顶层 thread，
          不重建）；terminal（completed/failed/cancelled）→ 原样返回（不重复 run）；
        - 顶层 run 只由 runner 执行（`_orchestration_runner`，production factory
          绑定）；未绑定 → RuntimeError（programming error）。
        """
        result = await self.create_or_get_orchestration(task_id)
        if result.status in ACTIVE_ORCHESTRATION_STATUSES:
            if self._orchestration_runner is None:
                raise RuntimeError("orchestration runner not bound")
            await self._orchestration_runner.run_orchestration(result.orchestration_id)
        return await self.get_orchestration(result.orchestration_id)

    # ------------------------------------------------------------------ retry

    async def retry_orchestration(self, orchestration_id: UUID) -> ResearchOrchestrationResult:
        """真正 user retry（7A.2B.2 spec C）：同 ResearchPlan 生成 NEW orchestration。

        与 `create_or_get_orchestration` 不同——即使 research input 完全相同（同
        fingerprint），也创建 **NEW orchestration_id + NEW top-level thread**：

        - 只允许 source status = failed / cancelled；completed / active
          （pending/running/waiting_human）→ `ResearchOrchestrationAlreadyFinished`；
        - 先 `verify_orchestration_integrity`（spec B：old orchestration + ResearchPlan
          交叉核对 + retry_of 一致性），**不重新调用 planner / LLM**；
        - 事务内 `get_by_id_for_update`（FOR UPDATE）锁 old 行串行化并发 retry：
          若已有 `latest.attempt_no > old.attempt_no` 且 `latest.retry_of ==
          old.id` → 返回 winner（并发 retry 最终只有一个 attempt=2）；
        - `new_attempt = max attempt + 1`；same task_id / research_plan_id /
          input_fingerprint；`retry_of = old orchestration_id`；status=pending
          phase=planning；old 行**完全不改**（attempt 1/2/3 并存历史）。
        """
        await self.verify_orchestration_integrity(orchestration_id)
        while True:
            async with self._sessionmaker() as session:
                repo = ResearchOrchestrationRepository(session)
                old = await repo.get_by_id_for_update(orchestration_id)
                if old is None:
                    raise ResearchOrchestrationNotFound()
                if old.status in (
                    OrchestrationStatus.COMPLETED.value,
                    OrchestrationStatus.PENDING.value,
                    OrchestrationStatus.RUNNING.value,
                    OrchestrationStatus.WAITING_HUMAN.value,
                ):
                    raise ResearchOrchestrationAlreadyFinished()
                if old.research_plan_id is None:
                    raise ResearchOrchestrationIntegrityError(
                        "research orchestration retry plan missing"
                    )

                latest = await repo.get_latest_for_plan(old.research_plan_id)
                if (
                    latest is not None
                    and latest.attempt_no > old.attempt_no
                    and latest.retry_of_orchestration_id == old.orchestration_id
                ):
                    # 并发 retry（同 old）已创建 attempt+1 → 返回 winner。
                    return self._to_result(latest, replayed=True)

                new_attempt = (latest.attempt_no if latest is not None else 0) + 1
                orchestration = ResearchOrchestrationModel(
                    task_id=old.task_id,
                    research_plan_id=old.research_plan_id,
                    attempt_no=new_attempt,
                    retry_of_orchestration_id=old.orchestration_id,
                    orchestration_schema_version=old.orchestration_schema_version,
                    orchestrator_name=old.orchestrator_name,
                    orchestrator_version=old.orchestrator_version,
                    status=OrchestrationStatus.PENDING.value,
                    current_phase=OrchestrationPhase.PLANNING.value,
                    input_fingerprint=old.input_fingerprint,
                    started_at=datetime.now(UTC),
                )
                try:
                    row, created = await repo.create_or_get(orchestration)
                except IntegrityError:
                    # 并发：另一个 retry 抢占了同一 (plan, new_attempt)，或 task
                    # 已有一个 active orchestration。
                    await session.rollback()
                    winner = await repo.get_by_plan_and_attempt(old.research_plan_id, new_attempt)
                    if winner is not None:
                        return self._to_result(winner, replayed=True)
                    active = await repo.get_active_for_task(old.task_id)
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

    async def get_current_orchestration(self, task_id: UUID) -> ResearchOrchestrationResult:
        """task 的 `current` 状态投影（spec U）：active 优先，否则最近一条。

        无任何 orchestration → `ResearchOrchestrationNotFound`（404）。
        """
        async with self._sessionmaker() as session:
            repo = ResearchOrchestrationRepository(session)
            orchestration = await repo.get_active_for_task(task_id)
            if orchestration is None:
                orchestration = await repo.get_latest_for_task(task_id)
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

            # 7A.2B.2 spec B：retry_of 必须同 task / research_plan（immutable
            # history，服务层 integrity，不做复杂 DB trigger）。
            if orchestration.retry_of_orchestration_id is not None:
                parent = await ResearchOrchestrationRepository(session).get_by_id(
                    orchestration.retry_of_orchestration_id
                )
                if parent is None:
                    raise ResearchOrchestrationIntegrityError(
                        "research orchestration retry parent missing"
                    )
                if parent.task_id != stored_task_id:
                    raise ResearchOrchestrationIntegrityError(
                        "research orchestration retry task identity mismatch"
                    )
                if parent.research_plan_id != orchestration.research_plan_id:
                    raise ResearchOrchestrationIntegrityError(
                        "research orchestration retry plan identity mismatch"
                    )

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

    # ------------------------------------------------------------------ human actions

    async def act_on_orchestration(
        self,
        orchestration_id: UUID,
        action: str,
        comment: str | None = None,
    ) -> ResearchOrchestrationResult:
        """人工裁决 action（spec N/O/P）：**仅 waiting_human** orchestration。

        - `approve` / `rewrite` / `research`：先把 immutable human decision 提交到
          exact Stage5 child（`resume_stage5_human`，decision 由 review service 校验
          枚举），再 `run_orchestration` 继续顶层——continuation 重入
          `run_or_resume_stage5` 重查 child 终态后条件路由：approve → complete /
          rewrite → 重新 awaiting_stage5（或再次 interrupt）/ research →
          pause_for_research **只持久化 research_request_id + phase=research_backflow**
          （spec P，不做 backflow 循环）；
        - `cancel`：委托 `cancel_orchestration`（幂等：已 cancelled 原样返回）；
        - 守卫：orchestration 不存在 → NotFound；status ≠ waiting_human →
          AlreadyFinished；phase ≠ awaiting_stage5（无 Stage5 child 待裁决）→
          InvalidAction；未知 action → InvalidAction；runners 未绑定 →
          RuntimeError（programming error）。
        """
        if action == HUMAN_DECISION_CANCEL:
            return await self.cancel_orchestration(orchestration_id)
        if action not in _HUMAN_RESUME_ACTIONS:
            raise ResearchOrchestrationInvalidAction(f"unsupported orchestration action: {action}")
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        if orchestration.status != OrchestrationStatus.WAITING_HUMAN.value:
            raise ResearchOrchestrationAlreadyFinished()
        if orchestration.current_phase != OrchestrationPhase.AWAITING_STAGE5.value:
            raise ResearchOrchestrationInvalidAction(
                "orchestration must be awaiting_stage5 to accept a human decision"
            )
        if self._stage5_runner is None or self._orchestration_runner is None:
            raise RuntimeError("orchestration action runners not bound")

        async with self._sessionmaker() as session:
            child = await ResearchOrchestrationChildRepository(session).get_child(
                orchestration_id, ChildStage.STAGE5.value, 1
            )
        if child is None:
            raise ResearchOrchestrationChildNotFound()

        await self._stage5_runner.resume_stage5_human(
            child.workflow_run_id, decision=action, comment=comment
        )
        await self._orchestration_runner.run_orchestration(orchestration_id)
        return await self.get_orchestration(orchestration_id)

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
            attempt_no=orchestration.attempt_no,
            retry_of_orchestration_id=orchestration.retry_of_orchestration_id,
            replayed=replayed,
        )


@dataclass(frozen=True)
class ChildRunResult:
    """orchestration → child run 的定位结果（exact run，不含 request 正文）。"""

    run_id: UUID
    created: bool


class ResearchOrchestrationChildService:
    """orchestration → child run 的 exact ownership（spec C/D/K，7A.2B.2 spec E/F/J/K）。

    **ensure_stage4_child / ensure_stage5_child**：exact child
    `(orchestration_id, stage, attempt 1)`——**绝不用 `latest task + graph_name`
    猜归属**。

    - 已存在 → 返回 exact run（recovery / re-run 复用，不重复 create）；
    - 不存在 → runner（stage4/stage5）`create_*_run(on_run_created=...)` **同一事务**
      创建 WorkflowRun + child link（spec K same-transaction：run create 与 link
      insert 之间无 crash 孤儿窗口）。hook 由本层提供——runner 不 import
      orchestration model（spec F layering）；
    - 并发：
      - active index 冲突 → runner 转 `ActiveWorkflowRunExists` → 重查 exact child
        → 命中返回 winner，否则原样抛；
      - child link 归属冲突（`UNIQUE(workflow_run_id)` /
        `UNIQUE(orchestration_id, stage, attempt_no)`）→ runner 原样抛
        IntegrityError → 先重查 exact child（并发 winner），无 winner 则分类为
        `ResearchOrchestrationChildConflict`（spec E，409）。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        runner: Stage4WorkflowRunner,
        stage5_runner: Stage5WorkflowRunner | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._runner = runner
        self._stage5_runner = stage5_runner

    async def ensure_stage4_child(
        self,
        orchestration_id: UUID,
        stage4_request: Stage4WorkflowRequest,
    ) -> ChildRunResult:
        return await self._ensure_child(orchestration_id, ChildStage.STAGE4.value, stage4_request)

    async def ensure_stage5_child(
        self,
        orchestration_id: UUID,
        stage5_request: Stage5WorkflowRequest,
    ) -> ChildRunResult:
        """exact child `(orchestration_id, stage5, attempt 1)`（spec K）。

        stage5 runner 必须绑定（production factory / graph 注入）；未绑定 →
        RuntimeError（programming error，不猜归属）。
        """
        if self._stage5_runner is None:
            raise RuntimeError("stage5 runner not bound to child service")
        return await self._ensure_child(orchestration_id, ChildStage.STAGE5.value, stage5_request)

    async def _ensure_child(
        self,
        orchestration_id: UUID,
        stage: str,
        request,
    ) -> ChildRunResult:
        """stage4/stage5 共用的 exact child 逻辑（create 分支按 stage 分发 runner）。"""
        async with self._sessionmaker() as session:
            child_repo = ResearchOrchestrationChildRepository(session)
            existing = await child_repo.get_child(orchestration_id, stage, 1)
            if existing is not None:
                run = await WorkflowRunRepository(session).get_by_id(existing.workflow_run_id)
                if run is None:
                    raise ResearchOrchestrationIntegrityError("orchestration child run missing")
                return ChildRunResult(run_id=run.run_id, created=False)

        def _attach_child(session: AsyncSession, run_id: UUID) -> None:
            session.add(
                ResearchOrchestrationChildModel(
                    orchestration_id=orchestration_id,
                    workflow_run_id=run_id,
                    stage=stage,
                    attempt_no=1,
                )
            )

        try:
            if stage == ChildStage.STAGE4.value:
                result = await self._runner.create_stage4_run(request, on_run_created=_attach_child)
            else:
                result = await self._stage5_runner.create_stage5_run(
                    request, on_run_created=_attach_child
                )
        except ActiveWorkflowRunExists:
            # 并发：另一个 active WorkflowRun（可能是本 orchestration 的 child）
            # 已存在 → exact 重查，命中返回 winner。
            winner = await self._requery_exact_child(orchestration_id, stage)
            if winner is not None:
                return winner
            raise
        except IntegrityError as exc:
            # child link 归属冲突（runner 原样抛）→ 先重查 exact child（并发
            # winner）；确无 child 且约束属于 child ownership → 409，否则原样抛。
            winner = await self._requery_exact_child(orchestration_id, stage)
            if winner is not None:
                return winner
            if _constraint_name(exc) in _CHILD_OWNERSHIP_CONSTRAINTS:
                raise ResearchOrchestrationChildConflict() from None
            raise
        return ChildRunResult(run_id=result.run_id, created=True)

    async def _requery_exact_child(
        self, orchestration_id: UUID, stage: str
    ) -> ChildRunResult | None:
        """并发 winner 判定：exact child `(orchestration_id, stage, attempt 1)`。"""
        async with self._sessionmaker() as session:
            existing = await ResearchOrchestrationChildRepository(session).get_child(
                orchestration_id, stage, 1
            )
        if existing is not None:
            return ChildRunResult(run_id=existing.workflow_run_id, created=False)
        return None
