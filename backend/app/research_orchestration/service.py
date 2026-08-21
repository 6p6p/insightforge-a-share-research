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
  spec P）；`cancel` 委托 `cancel_orchestration`。runners 未绑定 → RuntimeError；
- **resume_after_source_acquisition(orchestration_id)**（7A Product Gate spec
  J/K/L）：受控补资料后**同线程恢复**（不换顶层 thread / 不新建 orchestration）。
  服务端读 checkpoint 分类：waiting_manual → K1 prepare 重路由；research_backflow
  且 reason=source_acquisition_required（RESUME_BACKFLOW_MANUAL_REASONS 唯一成员）
  → K2 同 round 重跑补充研究；reason=structured_data_refresh_required → D2 拒绝
  （结构化 refresh 不在 automatic 文档补充研究范围）；reason=limit_reached → K3
  拒绝；awaiting_stage5 → L 拒绝（与 HumanReviewDecision 分开）。后台
  `schedule_resume`，返回投影供轮询。
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.audit.severity import (
    AuditImpactScope,
    classify_report_scope,
)
from app.core.errors import ActiveWorkflowRunExists
from app.core.logging import get_logger
from app.db.models.research_orchestration import (
    ResearchOrchestrationChildModel,
    ResearchOrchestrationModel,
)
from app.domain.tasks import ACTIVE_WORKFLOW_RUN_STATUSES
from app.draft_section.contracts import DEGRADED_SECTION_STATUS
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
    RESEARCH_BACKFLOW_LIMIT_REACHED,
    RESUME_BACKFLOW_MANUAL_REASONS,
    RESUME_KIND_PREPARE,
    RESUME_KIND_STAGE5_RETRY,
    RESUME_KIND_SUPPLEMENTAL_RESEARCH,
    STAGE5_AUDIT_DEGRADED_REASONS,
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
    ResearchOrchestrationRetryRequired,
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

if TYPE_CHECKING:  # pragma: no cover — 仅类型注解
    from app.research_orchestration.execution_manager import ResearchOrchestrationExecutionManager
    from app.research_orchestration.runner import ResearchOrchestrationRunner

logger = get_logger("app.research_orchestration_service")


def _uuid_or_none(value) -> UUID | None:
    """checkpoint state 的 id 是 str → UUID；非法 / None → None。"""
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _degraded_draft_ids(verified) -> frozenset[str]:
    """从 verified CheckResult 派生 degraded DraftSection id 集合（v1.2.3）。

    degraded 章节的 findings 一律归入 WARNING（与 finalize_on_approve 同规则）：
    半实证占位不因内容本身升级 critical。
    """
    return frozenset(
        draft.section_id
        for draft in verified.verified_report.verified_drafts
        if draft.status == DEGRADED_SECTION_STATUS
    )


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
class OrchestrationStartOutcome:
    """`prepare_orchestration_start` 的结果（route 据此决定 HTTP 状态码，Gate C）。

    - `orchestration`：返回的 orchestration 摘要；
    - `created`：True → 本次新建（route 201）；False → 已存在；
    - `scheduled`：True → 已调度后台运行（route 202）；False → 未调度
      （running / waiting_human / completed，route 200）。
    """

    orchestration: ResearchOrchestrationResult
    created: bool
    scheduled: bool


@dataclass(frozen=True)
class ResearchOrchestrationResult:
    """一次 top-level orchestration 的只读摘要（不含 plan / child 正文）。

    7A Product Gate spec O：checkpoint 派生字段（current_child_run_id /
    backflow_round / research_request_id / manual_reason / missing_need_codes）
    在 runner 绑定时从顶层 checkpoint 补充（`_project`），不放过 Evidence body /
    prompt / reasoning。
    """

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
    # 7A Product Gate spec O：checkpoint 派生（可空，未进入对应阶段 / runner 未绑定时 None）。
    current_child_run_id: UUID | None = None
    backflow_round: int | None = None
    research_request_id: UUID | None = None
    manual_reason: str | None = None
    missing_need_codes: list[str] | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class BackflowReviewView:
    """backflow manual closure 的只读投影（供 API / 前端按钮 disable 判断）。"""

    orchestration_id: UUID
    backflow_human_request_id: UUID | None = None
    reason: str | None = None
    decision: str | None = None
    comment: str | None = None
    decided_at: datetime | None = None
    impact_scope: str | None = None  # v1.2.4 impact scope (REPORT/SECTION/INFO)
    acceptance_barriers: list[str] = field(default_factory=list)


class ResearchOrchestrationService:
    """Top-level research orchestration 应用服务（create / read / verify / cancel）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        plan_service: ResearchPlanningService,
        stage5_runner: Stage5WorkflowRunner | None = None,
        orchestration_runner: ResearchOrchestrationRunner | None = None,
        execution_manager: ResearchOrchestrationExecutionManager | None = None,
        source_preparation: object | None = None,
        report_audit_service: object | None = None,
        report_check_service: object | None = None,
        closure_service: object | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._plan_service = plan_service
        # human action（spec N/P）：approve/rewrite/research 需要 Stage5 runner
        # resume child + 顶层 runner 继续 continuation；未绑定 → RuntimeError
        # （production factory 绑定，unit 测试可只测 dispatch 守卫）。
        self._stage5_runner = stage5_runner
        self._orchestration_runner = orchestration_runner
        # 后台调度（Gate B/C/E）：prepare_orchestration_start / retry_and_schedule /
        # cancel_orchestration 共用同一 manager；未绑定 → 不调度（unit 测试）。
        self._execution_manager = execution_manager
        # V1.1 P0-2：resume 前预准备公司 source（parse→chunk→index，best-effort
        # 后台）；未绑定（unit 测试）→ 跳过，编排图内 fulfill 仍可自愈。
        self._source_preparation = source_preparation
        # P0 backflow manual closure：accept 守卫用 report_audit/check verify；
        # closure_service 持久化人工审核请求与裁决。未绑定（unit 测试）→ 守卫
        # RuntimeError（不静默降级）。
        self._report_audit_service = report_audit_service
        self._report_check_service = report_check_service
        self._closure_service = closure_service

    @property
    def orchestration_runner(self) -> ResearchOrchestrationRunner | None:
        """只读：lifespan recovery coordinator / API 复用同一顶层 runner。

        production factory（`_create_research_orchestration`）绑定；unit 测试不绑
        → None（dispatch 守卫不触碰）。
        """
        return self._orchestration_runner

    @property
    def execution_manager(self) -> ResearchOrchestrationExecutionManager | None:
        """只读：lifespan close 复用同一 manager（cancel 本地 task 用）。"""
        return self._execution_manager

    async def _project(
        self, orchestration: ResearchOrchestrationModel
    ) -> ResearchOrchestrationResult:
        """row + 顶层 checkpoint 的完整状态投影（7A Product Gate spec O）。

        checkpoint 派生字段（current_child_run_id / backflow_round /
        research_request_id / backflow_manual_reason / missing_need_codes）只在
        `_orchestration_runner` 绑定时读取；runner 未绑定（unit 测试）或 checkpoint
        读取失败 → 这些字段保持 None（row 本身是权威，不因投影失败降级）。
        **不放过** Evidence body / prompt / reasoning。
        """
        base = self._to_result(orchestration)
        if self._orchestration_runner is None:
            return base
        try:
            checkpoint = await self._orchestration_runner.read_orchestration_checkpoint(
                orchestration.orchestration_id
            )
        except Exception as exc:
            logger.warning(
                "research_orchestration_checkpoint_read_failed",
                orchestration_id=str(orchestration.orchestration_id),
                error_type=type(exc).__name__,
            )
            return base
        return replace(
            base,
            current_child_run_id=_uuid_or_none(checkpoint.get("current_child_run_id")),
            backflow_round=checkpoint.get("backflow_round"),
            research_request_id=_uuid_or_none(checkpoint.get("research_request_id")),
            manual_reason=checkpoint.get("backflow_manual_reason"),
            missing_need_codes=checkpoint.get("missing_need_codes"),
            updated_at=(
                orchestration.updated_at.astimezone(UTC)
                if orchestration.updated_at is not None
                else None
            ),
        )

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

    async def prepare_orchestration_start(self, task_id: UUID) -> OrchestrationStartOutcome:
        """快速返回的自动研究入口（Gate C：**不 await 整个 LangGraph**）。

        语义（Case 1-6）：
        - Case 1  task 从未有 orchestration → create attempt1 + schedule O1；
        - Case 2  active=pending → 返回 exact active；本进程无 local task →
                  schedule exact active（恢复中断的调度）；
        - Case 3  active=running → 返回 active（不重复 schedule）；
        - Case 4  active=waiting_human → 返回 active（**不自动 resume**）；
        - Case 5  无 active、latest=completed → 返回 latest completed；
        - Case 6  无 active、latest=failed/cancelled → `ResearchOrchestrationRetryRequired`
                  （不偷偷回到 attempt1，用户必须显式 retry）。

        schedule 经 `ResearchOrchestrationExecutionManager`（同 id 至多一个
        background task）；manager 未绑定 → 不调度（unit 测试直接构造，production
        factory 绑定）。
        """
        async with self._sessionmaker() as session:
            repo = ResearchOrchestrationRepository(session)
            active = await repo.get_active_for_task(task_id)
            latest = None if active is not None else await repo.get_latest_for_task(task_id)

        if active is not None:
            result = await self._project(active)
            scheduled = False
            if (
                active.status == OrchestrationStatus.PENDING.value
                and self._execution_manager is not None
            ):
                scheduled = self._execution_manager.schedule(active.orchestration_id)
            return OrchestrationStartOutcome(
                orchestration=result, created=False, scheduled=scheduled
            )

        if latest is None:
            # Case 1：从未有 orchestration → 首次 create attempt1 + schedule。
            result = await self.create_or_get_orchestration(task_id)
            scheduled = (
                self._execution_manager.schedule(result.orchestration_id)
                if self._execution_manager is not None
                else False
            )
            return OrchestrationStartOutcome(
                orchestration=result, created=True, scheduled=scheduled
            )

        # 无 active：latest 只可能是 terminal（completed / failed / cancelled）。
        if latest.status in (
            OrchestrationStatus.COMPLETED.value,
            OrchestrationStatus.COMPLETED_WITH_WARNINGS.value,
        ):
            return OrchestrationStartOutcome(
                orchestration=await self._project(latest), created=False, scheduled=False
            )
        raise ResearchOrchestrationRetryRequired()

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
                    OrchestrationStatus.COMPLETED_WITH_WARNINGS.value,
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

    async def retry_and_schedule(self, orchestration_id: UUID) -> ResearchOrchestrationResult:
        """user retry → 创建 O2 **并自动 schedule**（Gate E；**不 await O2 完成**）。

        - 复用底层 `retry_orchestration(O1)`（同 ResearchPlan、attempt+1、
          `retry_of=O1`、same input_fingerprint、O1 原样保留；并发 retry 经
          FOR UPDATE 串行化 → 最终只有一个 O2，不会 O2+O3）；
        - 创建后立即 `ResearchOrchestrationExecutionManager.schedule(O2)`（同 id
          至多一个 background task）——**不需要第二次 API 调用再 start O2**；
        - 返回 O2 摘要（status=pending，立即返回）。
        """
        result = await self.retry_orchestration(orchestration_id)
        if self._execution_manager is not None:
            self._execution_manager.schedule(result.orchestration_id)
        return result

    # ------------------------------------------------------------------ read

    async def get_orchestration(self, orchestration_id: UUID) -> ResearchOrchestrationResult:
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        return await self._project(orchestration)

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
        return await self._project(orchestration)

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
        """minimal cancel（spec Q）+ ExecutionManager 协作式取消本地 task（Gate F）。

        **顺序**：先协作式取消本地 asyncio task（await 完成，Runner 的
        `_stream` 对 CancelledError 不投影失败）→ 再 DB 投影 cancelled（active
        child cancel + orchestration status=cancelled）。**不出现"只 cancel
        asyncio.Task 但 DB 仍 running"**；**不直接 SQL 删除 child / orchestration**
        （old child / history / checkpoint 保留）。幂等：已 cancelled → 原样返回；
        已 completed/failed → `ResearchOrchestrationAlreadyFinished`。
        """
        async with self._sessionmaker() as session:
            repo = ResearchOrchestrationRepository(session)
            orchestration = await repo.get_by_id(orchestration_id)
            if orchestration is None:
                raise ResearchOrchestrationNotFound()
            if orchestration.status == OrchestrationStatus.CANCELLED.value:
                return self._to_result(orchestration)
            if orchestration.status in (
                OrchestrationStatus.COMPLETED.value,
                OrchestrationStatus.COMPLETED_WITH_WARNINGS.value,
                OrchestrationStatus.FAILED.value,
            ):
                raise ResearchOrchestrationAlreadyFinished()

        if self._execution_manager is not None:
            await self._execution_manager.cancel_local(orchestration_id)

        now = datetime.now(UTC)
        async with self._sessionmaker() as session:
            repo = ResearchOrchestrationRepository(session)
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

        # 精确定位当前待裁决的 Stage5 child：首启 attempt 1；backflow 轮次后为
        # attempt = backflow_round + 1（spec D：exact (orchestration_id, stage5,
        # attempt_no)，不猜 latest）。
        checkpoint = await self._orchestration_runner.read_orchestration_checkpoint(
            orchestration_id
        )
        attempt_no = (checkpoint.get("backflow_round") or 0) + 1
        async with self._sessionmaker() as session:
            child = await ResearchOrchestrationChildRepository(session).get_child(
                orchestration_id, ChildStage.STAGE5.value, attempt_no
            )
        if child is None:
            raise ResearchOrchestrationChildNotFound()

        await self._stage5_runner.resume_stage5_human(
            child.workflow_run_id, decision=action, comment=comment
        )
        await self._orchestration_runner.run_orchestration(orchestration_id)
        return await self.get_orchestration(orchestration_id)

    async def resume_after_source_acquisition(
        self, orchestration_id: UUID
    ) -> ResearchOrchestrationResult:
        """受控补资料后同线程恢复（7A Product Gate spec J/K/L）。

        **仅 waiting_human** orchestration；服务端先读顶层 checkpoint 分类
        （错误**同步**抛给调用方），再后台 `schedule_resume`（K1/K2 可能跑完整
        Stage4，长任务不阻塞 API，前端轮询投影）：
        - phase=waiting_manual → **K1** kind=prepare：ensure_route → prepare 重算
          route_readiness（补资料后）→ ready→Stage4 attempt 1 | 仍缺→waiting_manual
          END（同 orchestration_id + 同顶层 thread，不换 thread）；
        - phase=research_backflow 且 `backflow_manual_reason` =
          source_acquisition_required（唯一 ∈ RESUME_BACKFLOW_MANUAL_REASONS）→
          **K2** kind=supplemental_research：同 research_request_id + 同
          backflow_round 重跑 execute_supplemental_research（不 round+1、不新建
          SupplementalPlan）；
        - reason=structured_data_refresh_required → **D2 拒绝**（InvalidAction：
          结构化 refresh 不在 automatic 文档补充研究范围，上传 PDF / URL 不能解决，
          resume 不得伪装成 document retrieval 已解决）；
        - reason=research_backflow_limit_reached → **K3 拒绝**（InvalidAction：
          MAX rounds 不可绕过，须 retry 新 orchestration）；
        - phase=awaiting_stage5（Stage5 人工裁决）→ **L：InvalidAction，与
          HumanReviewDecision 分开**（走 act_on_orchestration）。
        守卫：orchestration 不存在 → NotFound；status ≠ waiting_human →
        AlreadyFinished；runner / manager 未绑定 → RuntimeError（production
        factory 绑定）。
        """
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        if orchestration.status != OrchestrationStatus.WAITING_HUMAN.value:
            raise ResearchOrchestrationAlreadyFinished()
        if self._orchestration_runner is None or self._execution_manager is None:
            raise RuntimeError("orchestration resume runner not bound")

        checkpoint = await self._orchestration_runner.read_orchestration_checkpoint(
            orchestration_id
        )
        phase = checkpoint.get("current_phase") or orchestration.current_phase
        if phase == OrchestrationPhase.WAITING_MANUAL.value:
            kind = RESUME_KIND_PREPARE
        elif phase == OrchestrationPhase.RESEARCH_BACKFLOW.value:
            reason = checkpoint.get("backflow_manual_reason")
            if reason == RESEARCH_BACKFLOW_LIMIT_REACHED:
                raise ResearchOrchestrationInvalidAction(
                    "research backflow limit reached; retry with a new orchestration"
                )
            if reason not in RESUME_BACKFLOW_MANUAL_REASONS:
                raise ResearchOrchestrationInvalidAction(
                    f"research backflow manual reason not resumable: {reason or 'unknown'}"
                )
            kind = RESUME_KIND_SUPPLEMENTAL_RESEARCH
        else:
            # 含 awaiting_stage5（Stage5 人工裁决）——L：走 act_on_orchestration，
            # 不进入 source acquisition resume。
            raise ResearchOrchestrationInvalidAction(
                "orchestration is not waiting for source acquisition resume"
            )

        # V1.1 P0-2：resume 前预准备该公司全部未 parse 的 source（后台
        # best-effort）。即使预准备未完成/失败，编排图内 fulfill 的 index
        # builder 仍会自愈补建——这里只是让「继续研究」尽快进入就绪。
        if self._source_preparation is not None:
            try:
                company_id = await self._company_id_for_orchestration(orchestration)
                if company_id is not None:
                    self._source_preparation.schedule_prepare_company(company_id)
            except Exception as exc:  # noqa: BLE001 - 预准备失败不阻止 resume
                logger.warning(
                    "orchestration_resume_prepare_skipped",
                    orchestration_id=str(orchestration_id),
                    error_type=type(exc).__name__,
                )

        self._execution_manager.schedule_resume(orchestration_id, kind)
        return await self.get_orchestration(orchestration_id)

    async def _company_id_for_orchestration(self, orchestration) -> UUID | None:
        """orchestration → research_plan_id → plan.company_id（预准备用）。"""
        if orchestration.research_plan_id is None:
            return None
        from app.db.models.research_plan import ResearchPlanModel

        async with self._sessionmaker() as session:
            plan = await session.get(ResearchPlanModel, orchestration.research_plan_id)
            return plan.company_id if plan is not None else None

    # ------------------------------------------------------------------ backflow closure (P0)

    async def get_backflow_review(self, orchestration_id: UUID) -> BackflowReviewView:
        """closure 只读投影：request + decision + accept 守卫 barrier（按钮禁用理由）。"""
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id,
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        if self._closure_service is None:
            raise RuntimeError("backflow closure service not bound")
        request = await self._closure_service.get_request_for_orchestration(orchestration_id)
        if request is None:
            return BackflowReviewView(orchestration_id=orchestration_id)
        decision = await self._closure_service.get_decision_for_request(
            request.backflow_human_request_id,
        )
        barriers: list[str] = []
        impact_scope: str | None = None
        if decision is None:
            try:
                scope, barrier_list = await self._acceptance_evaluation(orchestration_id)
                impact_scope = scope.value
                barriers = barrier_list
            except RuntimeError:
                # 守卫服务未绑定（unit 测试 / 只读场景）→ 不计算 barrier/scope。
                barriers = []
                impact_scope = None
        return BackflowReviewView(
            orchestration_id=orchestration_id,
            backflow_human_request_id=request.backflow_human_request_id,
            reason=request.reason,
            decision=decision.decision if decision is not None else None,
            comment=decision.comment if decision is not None else None,
            decided_at=decision.decided_at if decision is not None else None,
            impact_scope=impact_scope,
            acceptance_barriers=barriers,
        )

    async def act_on_backflow_review(
        self,
        orchestration_id: UUID,
        decision: str,
        comment: str | None = None,
    ) -> ResearchOrchestrationResult:
        """backflow manual closure (P0): only waiting_human + research_backflow.

        decision in {accept / extra_research / cancel}:
        - accept: only when it holds no critical integrity failure (deterministic
          Check=pass AND no critical/high invalid issue) -> persist adjudication +
          orchestration completed;
        - extra_research: persist adjudication + schedule one bounded manual
          supplemental research round (reuse K2 same-thread resume; bounded);
        - cancel: persist adjudication + cancel_orchestration (clean terminal).
        Guards mirror act_on_orchestration (NotFound / AlreadyFinished /
        InvalidAction); a closure request must already exist (created by the
        research_backflow_manual node).
        """
        from app.research_backflow.closure import (
            BACKFLOW_DECISION_ACCEPT,
            BACKFLOW_DECISION_CANCEL,
            BACKFLOW_DECISION_EXTRA_RESEARCH,
            BACKFLOW_DECISIONS,
            BackflowReviewNotAcceptable,
        )

        if decision not in BACKFLOW_DECISIONS:
            raise ResearchOrchestrationInvalidAction(
                f"unsupported backflow review decision: {decision}"
            )
        if self._closure_service is None:
            raise RuntimeError("backflow closure service not bound")
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id,
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        if orchestration.status != OrchestrationStatus.WAITING_HUMAN.value:
            raise ResearchOrchestrationAlreadyFinished()
        if orchestration.current_phase != OrchestrationPhase.RESEARCH_BACKFLOW.value:
            raise ResearchOrchestrationInvalidAction(
                "orchestration must be research_backflow to run a closure action"
            )

        request = await self._closure_service.get_request_for_orchestration(orchestration_id)
        if request is None:
            raise ResearchOrchestrationIntegrityError("backflow closure request missing")

        if decision == BACKFLOW_DECISION_CANCEL:
            await self._closure_service.resolve_review(
                request.backflow_human_request_id,
                decision=BACKFLOW_DECISION_CANCEL,
                comment=comment,
            )
            return await self.cancel_orchestration(orchestration_id)

        if decision == BACKFLOW_DECISION_ACCEPT:
            scope, barriers = await self._acceptance_evaluation(orchestration_id)
            if barriers:
                raise BackflowReviewNotAcceptable(barriers)
            await self._closure_service.resolve_review(
                request.backflow_human_request_id,
                decision=BACKFLOW_DECISION_ACCEPT,
                comment=comment,
            )
            # v1.2.4：章节级缺陷（SECTION_WARNING / SECTION_UNAVAILABLE）接受 →
            # completed_with_warnings；无影响（INFO）→ completed。
            if scope is AuditImpactScope.INFO:
                async with self._sessionmaker() as session:
                    await ResearchOrchestrationRepository(session).mark_completed(
                        orchestration_id, datetime.now(UTC)
                    )
                    await session.commit()
            else:
                async with self._sessionmaker() as session:
                    await ResearchOrchestrationRepository(session).mark_completed_with_warnings(
                        orchestration_id, datetime.now(UTC)
                    )
                    await session.commit()
            return await self.get_orchestration(orchestration_id)

        # extra_research: manual continuation（bounded）：
        # - 常规 backflow → K2 补充研究轮（reuse RESUME_KIND_SUPPLEMENTAL_RESEARCH）；
        # - P0 audit-degraded（report_audit_* reasons）→ 重试 Stage5（新 attempt，
        #   RESUME_KIND_STAGE5_RETRY；draft/assemble fingerprint replay + 审计重试）。
        await self._closure_service.resolve_review(
            request.backflow_human_request_id,
            decision=BACKFLOW_DECISION_EXTRA_RESEARCH,
            comment=comment,
        )
        if self._orchestration_runner is None or self._execution_manager is None:
            raise RuntimeError("orchestration resume runner not bound")
        kind = (
            RESUME_KIND_STAGE5_RETRY
            if request.reason in STAGE5_AUDIT_DEGRADED_REASONS
            else RESUME_KIND_SUPPLEMENTAL_RESEARCH
        )
        self._execution_manager.schedule_resume(orchestration_id, kind)
        return await self.get_orchestration(orchestration_id)

    async def _acceptance_evaluation(
        self, orchestration_id: UUID
    ) -> tuple[AuditImpactScope, list[str]]:
        """accept 的确定性守卫：返回 (impact_scope, 不可接受的中文 barrier 列表)。

        - scope=REPORT_BLOCKING → 不可接受（barriers 非空，阻断 accept）；
        - scope=SECTION_WARNING / SECTION_UNAVAILABLE / INFO → 可接受（barriers 空，
          允许人工接受，按 scope 决定带警告完成或正常完成）。

        只拒绝 REPORT 级（unsupported numeric claim / provenance violation /
        data-truth 失败 / 确定性完整性失败）；章节级缺陷（degraded / unavailable /
        conflict_gap）不阻断。**模型不参与**——只读 verified Check + verified
        Audit issues。
        """
        barriers: list[str] = []
        checkpoint = await self._orchestration_runner.read_orchestration_checkpoint(
            orchestration_id
        )
        attempt_no = (checkpoint.get("backflow_round") or 0) + 1
        async with self._sessionmaker() as session:
            child = await ResearchOrchestrationChildRepository(session).get_child(
                orchestration_id, ChildStage.STAGE5.value, attempt_no
            )
        if child is None:
            return (AuditImpactScope.REPORT_BLOCKING, ["无法定位当前报告，不能接受"])
        if self._stage5_runner is None:
            raise RuntimeError("stage5 runner not bound for acceptance guard")
        stage5_state = await self._stage5_runner.read_checkpoint_state(child.workflow_run_id)
        audit_id = _uuid_or_none(stage5_state.get("audit_id"))
        check_result_id = _uuid_or_none(stage5_state.get("check_result_id"))
        if audit_id is None or check_result_id is None:
            # P0 UX 修复：守卫仍严格拒绝（无 audit/check 不可接受），但理由必须可理解：
            # 当前处于「自动审核失败 / 审核未完成」而非神秘「缺记录」。
            return (
                AuditImpactScope.REPORT_BLOCKING,
                [
                    "当前报告尚未完成自动审核（审核记录未生成），暂不能接受；"
                    "请选择「再次补充研究」重新验证，或取消研究。"
                ],
            )
        if self._report_check_service is None or self._report_audit_service is None:
            raise RuntimeError("acceptance guard services not bound")
        check = await self._report_check_service.verify_check_result_integrity(check_result_id)
        verified = await self._report_audit_service.verify_audit_integrity(audit_id)
        scope = classify_report_scope(
            finding_codes=[f.code for f in check.findings],
            finding_section_ids=[f.section_id for f in check.findings],
            issues=list(verified.issues),
            degraded_section_ids=_degraded_draft_ids(check),
        )
        if scope is AuditImpactScope.REPORT_BLOCKING:
            barriers.append("存在关键审核问题，不能接受当前报告")
        return (scope, barriers)

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
        *,
        attempt_no: int = _FIRST_ATTEMPT,
        source_research_request_id: UUID | None = None,
    ) -> ChildRunResult:
        return await self._ensure_child(
            orchestration_id,
            ChildStage.STAGE4.value,
            stage4_request,
            attempt_no=attempt_no,
            source_research_request_id=source_research_request_id,
        )

    async def ensure_stage5_child(
        self,
        orchestration_id: UUID,
        stage5_request: Stage5WorkflowRequest,
        *,
        attempt_no: int = _FIRST_ATTEMPT,
        source_research_request_id: UUID | None = None,
    ) -> ChildRunResult:
        """exact child `(orchestration_id, stage5, attempt_no)`（spec D/K）。

        v1 首启 attempt 1；backflow（7A.2B.3）用 attempt 2/3，child link 上记录
        source_research_request_id 供 audit 追踪。stage5 runner 必须绑定（production
        factory / graph 注入）；未绑定 → RuntimeError（programming error，不猜归属）。
        """
        if self._stage5_runner is None:
            raise RuntimeError("stage5 runner not bound to child service")
        return await self._ensure_child(
            orchestration_id,
            ChildStage.STAGE5.value,
            stage5_request,
            attempt_no=attempt_no,
            source_research_request_id=source_research_request_id,
        )

    async def _ensure_child(
        self,
        orchestration_id: UUID,
        stage: str,
        request,
        *,
        attempt_no: int = _FIRST_ATTEMPT,
        source_research_request_id: UUID | None = None,
    ) -> ChildRunResult:
        """stage4/stage5 共用的 exact child 逻辑（create 分支按 stage 分发 runner）。"""
        async with self._sessionmaker() as session:
            child_repo = ResearchOrchestrationChildRepository(session)
            existing = await child_repo.get_child(orchestration_id, stage, attempt_no)
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
                    attempt_no=attempt_no,
                    source_research_request_id=source_research_request_id,
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
            winner = await self._requery_exact_child(orchestration_id, stage, attempt_no)
            if winner is not None:
                return winner
            raise
        except IntegrityError as exc:
            # child link 归属冲突（runner 原样抛）→ 先重查 exact child（并发
            # winner）；确无 child 且约束属于 child ownership → 409，否则原样抛。
            winner = await self._requery_exact_child(orchestration_id, stage, attempt_no)
            if winner is not None:
                return winner
            if _constraint_name(exc) in _CHILD_OWNERSHIP_CONSTRAINTS:
                raise ResearchOrchestrationChildConflict() from None
            raise
        return ChildRunResult(run_id=result.run_id, created=True)

    async def _requery_exact_child(
        self, orchestration_id: UUID, stage: str, attempt_no: int = _FIRST_ATTEMPT
    ) -> ChildRunResult | None:
        """并发 winner 判定：exact child `(orchestration_id, stage, attempt_no)`。"""
        async with self._sessionmaker() as session:
            existing = await ResearchOrchestrationChildRepository(session).get_child(
                orchestration_id, stage, attempt_no
            )
        if existing is not None:
            return ChildRunResult(run_id=existing.workflow_run_id, created=False)
        return None
