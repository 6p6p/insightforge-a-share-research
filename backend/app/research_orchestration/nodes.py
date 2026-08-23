"""Top-level research orchestration graph nodes (stage 7A.2B.1 spec I/J + 7A.2B.2).

节点**复用**现有 services（ResearchPlanningService / ResearchSourceRouter /
ResearchPreparationService / ResearchFulfillmentService / Stage4WorkflowRunner /
SynthesisService / Stage5WorkflowRunner / ResearchOrchestrationChildService），
不复制业务逻辑（spec J）。每个节点是幂等的：graph 用 checkpointer 重放 /
resume 时，节点按需精确读取，不重复 create（plan / route / prepare replay；
Stage4/5 child 经 exact `get_child` attach/existing，spec D/K）。

- `_persist_phase`：短事务把 orchestration status + current_phase 投影到
  `research_orchestration_runs`（observability；graph 期间不持有 session）。
- `run_or_resume_stage4` / `run_or_resume_stage5`：按 child WorkflowRun 状态决定
  execute（pending）/ resume（failed(worker_restarted)）/ 跳过（completed /
  waiting_human / running，后者由恢复协调器处理）——**不重复 create child run**。
- Stage5 终态投影（spec L）：`run_or_resume_stage5` 把 child 终态写进 state
  （`stage5_run_status`），`route_stage5_result` 是**纯 state 路由**（不碰 DB，
  续接时 `aupdate_state` 注入 fresh 值后条件边重新判定，spec M）。
- child 内部失败（execute/resume 抛）由节点向上传播，顶层 runner 投影为
  orchestration failed（不吞 child 错误，spec M）。
"""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.audit.errors import (
    ReportAuditMalformedOutput,
    ReportAuditModelUnavailable,
    ReportAuditValidationError,
)
from app.domain.tasks import WorkflowRunStatus
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    BACKFLOW_REASON_AUDIT_MALFORMED_OUTPUT,
    BACKFLOW_REASON_AUDIT_MODEL_UNAVAILABLE,
    BACKFLOW_REASON_AUDIT_VALIDATION_FAILED,
    MAX_BACKFLOW_RESEARCH_ROUNDS,
    RESEARCH_BACKFLOW_LIMIT_REACHED,
    RESEARCH_BACKFLOW_NO_PROGRESS,
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.errors import ResearchOrchestrationIntegrityError
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.stage5.contracts import (
    STAGE5_TERMINAL_CANCELLED,
    STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
    Stage5RequestBuilder,
    Stage5WorkflowRequest,
)


async def _persist_phase(
    sessionmaker: async_sessionmaker,
    orchestration_id: str,
    status: str,
    phase: str,
) -> None:
    """短事务持久化 orchestration status + current_phase。"""
    async with sessionmaker() as session:
        await ResearchOrchestrationRepository(session).update_progress(
            UUID(orchestration_id), status=status, current_phase=phase
        )
        await session.commit()


def _phase_value(phase: OrchestrationPhase) -> str:
    return phase.value


# ------------------------------------------------------------------ plan / route


def make_ensure_plan_node(deps: ResearchOrchestrationDependencies):
    """ensure_plan：create/replay ResearchPlan v2 → 绑定 research_plan_id。"""

    async def ensure_plan(state) -> dict:
        plan_result = await deps.plan_service.create_plan(UUID(state["task_id"]))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.PLANNING),
        )
        return {
            "research_plan_id": str(plan_result.research_plan_id),
            "current_phase": _phase_value(OrchestrationPhase.PLANNING),
        }

    return ensure_plan


def make_ensure_route_node(deps: ResearchOrchestrationDependencies):
    """ensure_route：确定性 SourceRouter → route plan（0 LLM）。"""

    async def ensure_route(state) -> dict:
        await deps.router.route_research_plan(UUID(state["research_plan_id"]))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.ROUTING),
        )
        return {"current_phase": _phase_value(OrchestrationPhase.ROUTING)}

    return ensure_route


# ------------------------------------------------------------------ prepare


def make_prepare_node(deps: ResearchOrchestrationDependencies):
    """prepare：从现有 artifacts 解析 needs → ready_for_analysis / missing_need_codes。"""

    async def prepare(state) -> dict:
        result = await deps.preparation.prepare_research(UUID(state["research_plan_id"]))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.PREPARING),
        )
        return {
            "preparation_ready": result.ready_for_analysis,
            "missing_need_codes": [need.need_code for need in result.missing_needs],
            "current_phase": _phase_value(OrchestrationPhase.PREPARING),
        }

    return prepare


def route_readiness(state) -> str:
    """prepare → 条件边：ready → stage4 child；not ready → fulfill。"""
    return "ready" if state.get("preparation_ready") else "not_ready"


def route_readiness_after_fulfill(state) -> str:
    """prepare_again → 条件边：ready → stage4 child；否则 → waiting_manual END。"""
    return "ready" if state.get("preparation_ready") else "waiting_manual"


def make_fulfill_node(deps: ResearchOrchestrationDependencies):
    """fulfill：只消费 missing_needs 自动补证据（executor 确定性；不 live fetch）。"""

    async def fulfill(state) -> dict:
        await deps.fulfillment.fulfill_research_needs(UUID(state["research_plan_id"]))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.FULFILLING),
        )
        return {"current_phase": _phase_value(OrchestrationPhase.FULFILLING)}

    return fulfill


def make_prepare_again_node(deps: ResearchOrchestrationDependencies):
    """prepare_again：fulfill 后重跑 prepare（幂等），重新评估 readiness。"""

    async def prepare_again(state) -> dict:
        result = await deps.preparation.prepare_research(UUID(state["research_plan_id"]))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.PREPARING),
        )
        return {
            "preparation_ready": result.ready_for_analysis,
            "missing_need_codes": [need.need_code for need in result.missing_needs],
            "current_phase": _phase_value(OrchestrationPhase.PREPARING),
        }

    return prepare_again


# ------------------------------------------------------------------ stage4 child


def make_ensure_stage4_child_node(deps: ResearchOrchestrationDependencies):
    """ensure_stage4_child：exact child (orchestration_id, stage4, attempt 1)。

    ready 时才允许：re-run prepare 取得 `stage4_request`（状态只存 ID，不存
    request 正文，spec H），再经 `ResearchOrchestrationChildService.ensure_stage4_child`
    （同事务创建 WorkflowRun + child link；已有 child → attach，不重复 create）。
    """

    async def ensure_stage4_child(state) -> dict:
        prep = await deps.preparation.prepare_research(UUID(state["research_plan_id"]))
        if not prep.ready_for_analysis or prep.stage4_request is None:
            raise ResearchOrchestrationIntegrityError("stage4 child requires ready preparation")
        child = await deps.child_service.ensure_stage4_child(
            UUID(state["orchestration_id"]), prep.stage4_request
        )
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.STAGE4),
        )
        # stage4_child_run_id 是 immutable 锚点（Stage5 request 重建 + continuation），
        # current_child_run_id 在 ensure_stage5_child 之后指向 stage5 run。
        return {
            "stage4_child_run_id": str(child.run_id),
            "current_child_run_id": str(child.run_id),
            "current_phase": _phase_value(OrchestrationPhase.STAGE4),
        }

    return ensure_stage4_child


def make_run_or_resume_stage4_node(deps: ResearchOrchestrationDependencies):
    """run_or_resume_stage4：执行 / 恢复精确 Stage4 child run。

    - pending → `execute_stage4`（首启）；
    - failed(worker_restarted) → `resume_stage4`（同 run / thread 从 checkpoint
      恢复，synthesis 幂等 → 无重复产物）；
    - completed → 跳过（collect_synthesis 直接读 checkpoint）；
    - running → 已有 executor 在跑 / 恢复协调器处理，不重复执行。
    Stage4 失败向上传播（顶层 runner 投影 orchestration failed，不吞错误）。
    """

    async def run_or_resume_stage4(state) -> dict:
        run_id = UUID(state["current_child_run_id"])
        prep = await deps.preparation.prepare_research(UUID(state["research_plan_id"]))
        if prep.stage4_request is None:
            raise ResearchOrchestrationIntegrityError("stage4 child requires ready preparation")
        async with deps.sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None:
            raise ResearchOrchestrationIntegrityError("orchestration child run missing")
        if run.status == WorkflowRunStatus.PENDING.value:
            await deps.stage4_runner.execute_stage4(run_id, prep.stage4_request)
        elif run.status == WorkflowRunStatus.FAILED.value:
            await deps.stage4_runner.resume_stage4(run_id)
        # completed / running：不重复执行。
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.STAGE4),
        )
        return {"current_phase": _phase_value(OrchestrationPhase.STAGE4)}

    return run_or_resume_stage4


def make_collect_synthesis_node(deps: ResearchOrchestrationDependencies):
    """collect_synthesis：读真实 Stage4 checkpoint → verify SynthesisResult 完整性。

    只把 `synthesis_result_id` 投影进顶层 state（不复制 synthesis body，spec H/M）。
    verify 失败（SynthesisError → integrity）向上传播 → orchestration failed。
    phase 保持 stage4（synthesis 是 Stage4 产物；Stage5 由下一节点接管）。
    """

    async def collect_synthesis(state) -> dict:
        run_id = UUID(state["stage4_child_run_id"])
        final_state = await deps.stage4_runner.read_checkpoint_state(run_id)
        synthesis_id = final_state.get("synthesis_id")
        synthesis_result_id = final_state.get("synthesis_result_id")
        if synthesis_id is None or synthesis_result_id is None:
            raise ResearchOrchestrationIntegrityError("stage4 synthesis result missing")
        async with deps.sessionmaker() as session:
            await deps.synthesis_service.verify_synthesis_integrity(session, UUID(synthesis_id))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.STAGE4),
        )
        return {
            "synthesis_result_id": str(synthesis_result_id),
            "current_phase": _phase_value(OrchestrationPhase.STAGE4),
        }

    return collect_synthesis


# ------------------------------------------------------------------ stage5 child

# `stage5_run_status` 路由取值（route_stage5_result 判定；child run 状态 +
# checkpoint terminal 投影，spec L）。
STAGE5_ROUTE_COMPLETED = "completed"
# v1.2.2: stage5 finalize_with_warnings -> orchestration completed_with_warnings
STAGE5_ROUTE_COMPLETED_WITH_WARNINGS = "completed_with_warnings"
STAGE5_ROUTE_WAITING_HUMAN = "waiting_human"
STAGE5_ROUTE_RESEARCH_REQUIRED = "research_required"
STAGE5_ROUTE_FAILED = "failed"
STAGE5_ROUTE_CANCELLED = "cancelled"
# P0 degradation: audit 创建失败（有界纠正重试耗尽 / 模型不可用 / 输出畸形；
# report+check 已生成）→ 路由到 research_backflow_manual 人工闭环（接受被
# 确定性拒绝——无 audit 记录；可再次补充研究 = 重试 Stage5，或取消）。
STAGE5_ROUTE_AUDIT_DEGRADED = "audit_degraded"
_STAGE5_ROUTE_VALUES = frozenset(
    {
        STAGE5_ROUTE_COMPLETED,
        STAGE5_ROUTE_COMPLETED_WITH_WARNINGS,
        STAGE5_ROUTE_WAITING_HUMAN,
        STAGE5_ROUTE_RESEARCH_REQUIRED,
        STAGE5_ROUTE_FAILED,
        STAGE5_ROUTE_CANCELLED,
        STAGE5_ROUTE_AUDIT_DEGRADED,
    }
)


_AUDIT_TERMINAL_ERRORS = (
    ReportAuditModelUnavailable,
    ReportAuditMalformedOutput,
    ReportAuditValidationError,
)


def _audit_degraded_reason(exc: Exception) -> str:
    """audit 终态失败 → 稳定的 backflow_manual reason（前端 label + 幂等闭环）。"""
    if isinstance(exc, ReportAuditValidationError):
        return BACKFLOW_REASON_AUDIT_VALIDATION_FAILED
    if isinstance(exc, ReportAuditMalformedOutput):
        return BACKFLOW_REASON_AUDIT_MALFORMED_OUTPUT
    return BACKFLOW_REASON_AUDIT_MODEL_UNAVAILABLE


async def _stage5_request(deps: ResearchOrchestrationDependencies, state) -> Stage5WorkflowRequest:
    """从 Stage4 checkpoint 投影 Stage5 request（spec J：Stage5RequestBuilder）。

    `stage4_child_run_id` 是 immutable 锚点——首启与 continuation 共用同一投影，
    不复制 legacy `ResearchExecutionService` 的 bridge 逻辑。
    """
    stage4_state = await deps.stage4_runner.read_checkpoint_state(
        UUID(state["stage4_child_run_id"])
    )
    return Stage5RequestBuilder.from_stage4_state(
        task_id=UUID(state["task_id"]),
        stage4_state=stage4_state,
        synthesis_result_id=UUID(state["synthesis_result_id"]),
    )


def _require_backflow(deps: ResearchOrchestrationDependencies):
    """backflow loop 节点：deps 未装配 → RuntimeError（programming error，不静默降级）。"""
    if deps.backflow_service is None or deps.backflow_executor is None:
        raise RuntimeError("research backflow dependencies not bound")
    return deps.backflow_service, deps.backflow_executor


async def _stage5_request_for_attempt(
    deps: ResearchOrchestrationDependencies, state
) -> tuple[Stage5WorkflowRequest, int]:
    """Stage5 request + attempt_no：backflow 激活（round>0）→ continuation request
    （spec O：`build_stage5_continuation_request(fulfillment_id)`）+ attempt=round+1；
    否则从 Stage4 checkpoint 投影 + attempt=1。caller（ensure_stage5_child /
    run_or_resume_stage5）共用，保证 execute 与 request 一致。
    """
    if (state.get("backflow_round") or 0) > 0:
        service, _ = _require_backflow(deps)
        return (
            await service.build_stage5_continuation_request(UUID(state["fulfillment_id"])),
            state["backflow_round"] + 1,
        )
    # P0 degradation retry：audit 创建失败经人工"再次补充研究"重试 Stage5——新
    # attempt = stage5_retry_count + 1（有界，MAX_STAGE5_DEGRADED_RETRY_ROUNDS）。
    retry_count = state.get("stage5_retry_count") or 0
    return (await _stage5_request(deps, state), 1 + retry_count)


async def _stage5_outcome(
    deps: ResearchOrchestrationDependencies, run_id: UUID
) -> tuple[str, dict]:
    """投影 Stage5 child 当前终态 → (stage5_run_status, extra_state)。

    - waiting_human / failed / cancelled：直接取 child run 状态；
    - completed：从 checkpoint terminal 区分 finalize（completed）/
      research_required（+ research_request_id，spec P）/ cancelled；
    - running：live executor / rolling restart——恢复协调器负责跳过，这里防御性
      抛 integrity error，避免把 running 误判为终态。
    """
    async with deps.sessionmaker() as session:
        run = await WorkflowRunRepository(session).get_by_id(run_id)
    if run is None:
        raise ResearchOrchestrationIntegrityError("orchestration child run missing")
    status = run.status
    if status == WorkflowRunStatus.WAITING_HUMAN.value:
        return STAGE5_ROUTE_WAITING_HUMAN, {}
    if status == WorkflowRunStatus.FAILED.value:
        return STAGE5_ROUTE_FAILED, {}
    if status == WorkflowRunStatus.CANCELLED.value:
        return STAGE5_ROUTE_CANCELLED, {}
    if status == WorkflowRunStatus.RUNNING.value:
        raise ResearchOrchestrationIntegrityError("stage5 child running during orchestration run")
    checkpoint = await deps.stage5_runner.read_checkpoint_state(run_id)
    terminal = checkpoint.get("terminal")
    if terminal == STAGE5_TERMINAL_RESEARCH_REQUIRED:
        return STAGE5_ROUTE_RESEARCH_REQUIRED, {
            "research_request_id": checkpoint.get("research_request_id")
        }
    if terminal == STAGE5_TERMINAL_CANCELLED:
        return STAGE5_ROUTE_CANCELLED, {}
    if terminal == STAGE5_TERMINAL_FINALIZE_WITH_WARNINGS:
        # v1.2.2: 人工批准带警告完成 -> 路由到 completed_with_warnings 终态。
        return STAGE5_ROUTE_COMPLETED_WITH_WARNINGS, {}
    return STAGE5_ROUTE_COMPLETED, {}


def make_ensure_stage5_child_node(deps: ResearchOrchestrationDependencies):
    """ensure_stage5_child：exact child (orchestration_id, stage5, attempt_no)。

    - 首启（round=0）：从真实 Stage4 checkpoint 投影 `Stage5WorkflowRequest`
      （spec J/K），不复制 Stage4→Stage5 bridge 逻辑；attempt 1；
    - backflow（round>0，7A.2B.3）：`build_stage5_continuation_request(fulfillment_id)`
      构造续跑 request（spec O），attempt = round + 1，child link 记录
      source_research_request_id；
    - 经 `ResearchOrchestrationChildService.ensure_stage5_child` **同一事务**创建
      WorkflowRun + child link（已有 child → attach，不重复 create，spec K）。
    """

    async def ensure_stage5_child(state) -> dict:
        stage5_request, attempt_no = await _stage5_request_for_attempt(deps, state)
        is_backflow = (state.get("backflow_round") or 0) > 0
        child = await deps.child_service.ensure_stage5_child(
            UUID(state["orchestration_id"]),
            stage5_request,
            attempt_no=attempt_no,
            source_research_request_id=(
                UUID(state["research_request_id"]) if is_backflow else None
            ),
        )
        phase = (
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW)
            if is_backflow
            else _phase_value(OrchestrationPhase.STAGE5)
        )
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            phase,
        )
        return {
            "current_child_run_id": str(child.run_id),
            "current_phase": phase,
        }

    return ensure_stage5_child


def make_run_or_resume_stage5_node(deps: ResearchOrchestrationDependencies):
    """run_or_resume_stage5：执行 / 恢复精确 Stage5 child run，投影终态。

    - pending → `execute_stage5`（首启）；failed(worker_restarted) →
      `resume_stage5_for_recovery`；completed / waiting_human / cancelled → 跳过
      （waiting_human 已是 graph interrupt 暂停，等人工裁决；cancelled 已是终态）；
    - 无论执行与否，都从 child run 状态 + Stage5 checkpoint terminal 投影
      `stage5_run_status`（research_required 时同时投影 `research_request_id`，
      spec P），供 `route_stage5_result` 纯 state 路由（spec L/M）。
    """

    async def run_or_resume_stage5(state) -> dict:
        run_id = UUID(state["current_child_run_id"])
        stage5_request, _ = await _stage5_request_for_attempt(deps, state)
        async with deps.sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None:
            raise ResearchOrchestrationIntegrityError("orchestration child run missing")
        try:
            if run.status == WorkflowRunStatus.PENDING.value:
                await deps.stage5_runner.execute_stage5(run_id, stage5_request)
            elif run.status == WorkflowRunStatus.FAILED.value:
                # request 用于"crash 在 execute_stage5 前"的 run（无 checkpoint）：
                # resume 方法复用同 run/thread 重新首启。
                await deps.stage5_runner.resume_stage5_for_recovery(run_id, request=stage5_request)
        except _AUDIT_TERMINAL_ERRORS as exc:
            # P0 degradation: audit 创建失败, 不再把整个 Stage5 打成 execution_failed;
            # 路由到 research_backflow_manual 人工闭环（接受被确定性拒绝）。
            return {
                "stage5_run_status": STAGE5_ROUTE_AUDIT_DEGRADED,
                "backflow_manual_reason": _audit_degraded_reason(exc),
                "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
            }
        # completed / waiting_human / cancelled / running：不重复执行。

        status, extra = await _stage5_outcome(deps, run_id)
        phase = (
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW)
            if (state.get("backflow_round") or 0) > 0
            else _phase_value(OrchestrationPhase.STAGE5)
        )
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            phase,
        )
        return {
            "stage5_run_status": status,
            "current_phase": phase,
            **extra,
        }

    return run_or_resume_stage5


def route_stage5_result(state) -> str:
    """run_or_resume_stage5 → 条件边（**纯 state 路由**，不碰 DB，spec L/M）。

    continuation（spec M）：runner 用 `aupdate_state(config, {"current_phase":
    stage5}, as_node="ensure_stage5_child")` 重新进入 `run_or_resume_stage5`
    后，节点重新投影 fresh `stage5_run_status`，本函数据此重新判定。

    research_required（7A.2B.3）：`backflow_round < MAX_BACKFLOW_RESEARCH_ROUNDS`
    → 返回 `research_required`（进入 backflow loop）；否则 → `research_backflow_manual`
    （达到上限，reason=research_backflow_limit_reached）。
    """
    status = state.get("stage5_run_status")
    if status == STAGE5_ROUTE_RESEARCH_REQUIRED:
        if (state.get("backflow_round") or 0) >= MAX_BACKFLOW_RESEARCH_ROUNDS:
            return "research_backflow_manual"
        return STAGE5_ROUTE_RESEARCH_REQUIRED
    if status == STAGE5_ROUTE_AUDIT_DEGRADED:
        # P0 degradation：audit 创建失败 → 复用 research_backflow_manual 人工闭环
        # 节点（reason=backflow_manual_reason，由 run_or_resume_stage5 注入）。
        return "research_backflow_manual"
    if status in _STAGE5_ROUTE_VALUES:
        return status
    raise ValueError(f"invalid stage5_run_status: {status}")


# ------------------------------------------------------------------ terminals


def make_awaiting_stage5_node(deps: ResearchOrchestrationDependencies):
    """awaiting_stage5：Stage5 child 已 WAITING_HUMAN（graph interrupt）→ 顶层
    status=waiting_human、phase=awaiting_stage5，graph 到 END 暂停（spec M）。
    人工裁决后由 runner continuation 重新进入；幂等重放。"""

    async def awaiting_stage5(state) -> dict:
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.WAITING_HUMAN.value,
            _phase_value(OrchestrationPhase.AWAITING_STAGE5),
        )
        return {"current_phase": _phase_value(OrchestrationPhase.AWAITING_STAGE5)}

    return awaiting_stage5


def make_complete_orchestration_node(deps: ResearchOrchestrationDependencies):
    """complete_orchestration：Stage5 finalize → orchestration completed。"""

    async def complete_orchestration(state) -> dict:
        async with deps.sessionmaker() as session:
            await ResearchOrchestrationRepository(session).mark_completed(
                UUID(state["orchestration_id"]), datetime.now(UTC)
            )
            await session.commit()
        return {"current_phase": _phase_value(OrchestrationPhase.COMPLETED)}

    return complete_orchestration


def make_complete_orchestration_with_warnings_node(deps: ResearchOrchestrationDependencies):
    """complete_orchestration_with_warnings：v1.2.2 人工批准带警告完成。

    Stage5 finalize_with_warnings → orchestration status=completed_with_warnings
    （terminal；product 语义 = 研究完成且包含审核提醒，不是普通 completed）。
    """

    async def complete_orchestration_with_warnings(state) -> dict:
        async with deps.sessionmaker() as session:
            await ResearchOrchestrationRepository(session).mark_completed_with_warnings(
                UUID(state["orchestration_id"]), datetime.now(UTC)
            )
            await session.commit()
        return {"current_phase": _phase_value(OrchestrationPhase.COMPLETED)}

    return complete_orchestration_with_warnings


# ------------------------------------------------------------------ backflow loop（7A.2B.3）


def make_plan_supplemental_research_node(deps: ResearchOrchestrationDependencies):
    """plan_supplemental_research：round+1，create/replay 确定性补充计划（spec K）。

    `ResearchBackflowService.create_or_get_plan`（0 LLM 派生 need_specs[]，同
    request → replay）；`backflow_round` 递增表示进入一轮 backflow（Stage4/5
    child attempt = round + 1）。
    """

    async def plan_supplemental_research(state) -> dict:
        service, _ = _require_backflow(deps)
        round_no = (state.get("backflow_round") or 0) + 1
        plan = await service.create_or_get_plan(UUID(state["research_request_id"]))
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        return {
            "backflow_round": round_no,
            "backflow_plan_id": str(plan.backflow_plan_id),
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        }

    return plan_supplemental_research


def make_execute_supplemental_research_node(deps: ResearchOrchestrationDependencies):
    """execute_supplemental_research：确定性检索已有 Source Library → 新证据卡。

    verify request（重放校验）→ create_or_get_plan（幂等 replay 拿 plan_payload）
    → `ResearchBackflowExecutor.execute_supplemental_research`（真实检索链，只研究
    已有 source；无满足 source → manual_required）。投影**新增** EvidenceCard id
    供 verify_progress / prepare_updated_analysis。
    """

    async def execute_supplemental_research(state) -> dict:
        service, executor = _require_backflow(deps)
        verified = await service.verify_research_request_integrity(
            UUID(state["research_request_id"])
        )
        plan = await service.create_or_get_plan(UUID(state["research_request_id"]))
        result = await executor.execute_supplemental_research(verified, plan.plan_payload)
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        # 7A Product Gate spec I：executor 级 per-need manual reasons 聚合投影
        # （保留首次出现的稳定顺序；不保存 attempt 明细 / query）。与 plan 级
        # `backflow_manual_reasons` 分开——verify_progress 理由优先级：
        # plan reasons > executor reasons > research_backflow_no_progress。
        executor_reasons = [
            attempt.manual_required_reason
            for attempt in result.attempts
            if attempt.manual_required_reason is not None
        ]
        return {
            "backflow_new_evidence_card_ids": [
                str(card_id) for card_id in result.new_evidence_card_ids
            ],
            # plan 派生时检测到的 structured 需求（真正结构化缺口，不在 automatic
            # 文档补充研究范围）→ 投影给 verify_progress，使其给稳定 manual reason
            # （structured_data_refresh_required）而非误报 research_backflow_no_progress。
            "backflow_manual_reasons": plan.plan_payload.get("manual_required_reasons", []),
            # 非阻断数据缺口（措辞/表示类 normal 缺口）：报告继续完成，缺口在
            # Audit/Review 中保持可见（见 derive.py 分类说明）。
            "backflow_non_blocking_gap_issues": plan.plan_payload.get(
                "non_blocking_gap_issues", []
            ),
            "backflow_executor_manual_reasons": executor_reasons,
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        }

    return execute_supplemental_research


def make_verify_progress_node(deps: ResearchOrchestrationDependencies):
    """verify_progress：新增 relevant EvidenceCard → 有进度；否则 → manual_required。

    v1 只判定 executor 产出的新增 EvidenceCard（spec：新增 EvidenceCard **或**新增
    verified deterministic artifact 且新 Stage4 SynthesisResult ≠ 旧且新 run
    fingerprint ≠ 旧）。backflow executor 只产出 EvidenceCard；deterministic
    artifact（macro/calculation/valuation）是 production pools 的既有产物，非本
    loop 新增——分支 B 预留。
    **7A Product Gate spec C**：plan 级 `backflow_manual_reasons`（structured
    financial/macro/valuation refresh 需求）**恒常优先，不随 has_progress 翻转**。
    plan 同时含 document 自动 need + structured manual need 时，executor 产出新
    Document EvidenceCard 只是 document 侧进度，**不**代表 structured 缺口已解决
    （C3：document evidence 保留、不 rollback，但最终 waiting_human
    manual_reason=structured_data_refresh_required，不得进入 Stage4 next attempt /
    fulfillment / Stage5 continuation）。纯 document 进度 → 继续 Stage4 next attempt。
    """

    async def verify_progress(state) -> dict:
        has_progress = bool(state.get("backflow_new_evidence_card_ids"))
        # manual_required 稳定 reason 优先级（恒常，不随 has_progress 翻转）：
        #   plan 级 structured 需求（backflow_manual_reasons）>
        #   纯 document 进度（has_progress）>
        #   非阻断数据缺口（non_blocking_gap_issues：措辞/表示类 normal 缺口，
        #     audit 已展示，报告继续完成）>
        #   executor 级 manual reasons（backflow_executor_manual_reasons，7A Product
        #     Gate spec I/J：缺 eligible source → source_acquisition_required、
        #     index/evidence 未就绪 → 对应 reason）>
        #   research_backflow_no_progress。
        # 不误报 no_progress——resume_after_source_acquisition 依此区分
        # source_acquisition_required（可补资料后同线程恢复）与 genuine no-progress。
        manual_reasons = state.get("backflow_manual_reasons") or []
        executor_reasons = state.get("backflow_executor_manual_reasons") or []
        non_blocking_gaps = state.get("backflow_non_blocking_gap_issues") or []
        if manual_reasons:
            can_advance = False
            manual_reason = manual_reasons[0]
        elif has_progress:
            can_advance = True
            manual_reason = None
        elif non_blocking_gaps:
            # 非关键数据缺口：不阻断——报告继续完成（Stage4 → Stage5 → 新 audit），
            # 缺口在 Audit/Review 中保持可见。文档侧无新证据也不误报 no_progress。
            can_advance = True
            manual_reason = None
        elif executor_reasons:
            can_advance = False
            manual_reason = executor_reasons[0]
        else:
            can_advance = False
            manual_reason = RESEARCH_BACKFLOW_NO_PROGRESS
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        return {
            "backflow_progress": can_advance,
            "backflow_manual_reason": manual_reason,
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        }

    return verify_progress


def route_backflow_progress(state) -> str:
    """verify_progress → 条件边：有进度 → Stage4 attempt；无 → manual_required。"""
    return "progress" if state.get("backflow_progress") else "no_progress"


def route_after_collect_synthesis(state) -> str:
    """collect_synthesis → 条件边：首启（round=0）→ ensure_stage5_child；backflow
    （round>0）→ fulfill_request（先消费新 SynthesisResult 再重建 Stage5 续跑）。"""
    return "fulfill_request" if (state.get("backflow_round") or 0) > 0 else "ensure_stage5_child"


def make_prepare_updated_analysis_node(deps: ResearchOrchestrationDependencies):
    """prepare_updated_analysis：重跑 prepare（新卡经 research_question_sha256
    自动进 doc_evidence_pool → stage4_request，无需新 request-builder）→ ensure
    Stage4 child attempt round+1，`stage4_child_run_id` 锚点更新为最新 Stage4 run
    （collect_synthesis 读它得到新 SynthesisResult）。
    """

    async def prepare_updated_analysis(state) -> dict:
        prep = await deps.preparation.prepare_research(UUID(state["research_plan_id"]))
        if not prep.ready_for_analysis or prep.stage4_request is None:
            raise ResearchOrchestrationIntegrityError(
                "backflow stage4 child requires ready preparation"
            )
        attempt_no = (state.get("backflow_round") or 0) + 1
        child = await deps.child_service.ensure_stage4_child(
            UUID(state["orchestration_id"]),
            prep.stage4_request,
            attempt_no=attempt_no,
            source_research_request_id=UUID(state["research_request_id"]),
        )
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        return {
            "stage4_child_run_id": str(child.run_id),
            "current_child_run_id": str(child.run_id),
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        }

    return prepare_updated_analysis


def make_fulfill_request_node(deps: ResearchOrchestrationDependencies):
    """fulfill_request：消费新 Stage4 SynthesisResult → fulfillment 行（spec K/L/M/N）。

    `fulfill_request(research_request_id, new_synthesis_result_id)`：verify request
    + verify 新 result + continuation identity（company/question/cutoff 全等）+
    no-progress 政策（新 result ≠ source result 且新 run fingerprint ≠ source）+
    fingerprint → create_or_get。产物 `fulfillment_id` 供 Stage5 continuation
    request 重建。
    """

    async def fulfill_request(state) -> dict:
        service, _ = _require_backflow(deps)
        fulfillment = await service.fulfill_request(
            UUID(state["research_request_id"]), UUID(state["synthesis_result_id"])
        )
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        return {
            "fulfillment_id": str(fulfillment.fulfillment_id),
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        }

    return fulfill_request


def make_research_backflow_manual_node(deps: ResearchOrchestrationDependencies):
    """research_backflow_manual：backflow 终止 → status=waiting_human、phase=
    research_backflow、稳定 reason（research_backflow_no_progress 由 verify_progress
    投影；research_backflow_limit_reached 由 round 上限路由触发，节点默认）。"""

    async def research_backflow_manual(state) -> dict:
        reason = state.get("backflow_manual_reason") or RESEARCH_BACKFLOW_LIMIT_REACHED
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.WAITING_HUMAN.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        if deps.closure_service is not None:
            await deps.closure_service.create_or_get_review(
                UUID(state["orchestration_id"]),
                reason=reason,
                request_payload={
                    "backflow_round": state.get("backflow_round"),
                    "missing_need_codes": list(state.get("missing_need_codes") or []),
                    "non_blocking_gap_count": len(
                        state.get("backflow_non_blocking_gap_issues") or []
                    ),
                },
            )
        return {
            "backflow_manual_reason": reason,
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
            # v1.2.6-B（任务2）：backflow 终结时报告 artifact 已生成（stage5
            # terminal research_backflow 时 report_id 先于 research_required 落盘），
            # 仅存在资料/来源不足缺口 -> 记录 data_source_warning，前端据此提醒
            # 「部分资料缺失，相关章节需要人工确认」；仅当无任何报告（waiting_manual
            # 路径，0 WorkflowRun）才等待人工补资料，此时无此字段。
            "data_source_warning": (
                "部分资料缺失，相关章节需要人工确认（报告按现有证据完成，含审核提醒）"
            ),
        }

    return research_backflow_manual


def make_stage5_failed_node(deps: ResearchOrchestrationDependencies):
    """stage5_failed：child run 已 FAILED（业务终态，如 revision_limit_exceeded）
    → orchestration failed、phase=stage5、error_code=stage5_execution_failed
    （child 自身 run 的 error_code/message 已在 WorkflowRun 行上，不吞错误）。"""

    async def stage5_failed(state) -> dict:
        async with deps.sessionmaker() as session:
            await ResearchOrchestrationRepository(session).mark_failed(
                UUID(state["orchestration_id"]),
                datetime.now(UTC),
                error_code="stage5_execution_failed",
                error_message="stage5 child run failed",
            )
            await session.commit()
        return {"current_phase": _phase_value(OrchestrationPhase.STAGE5)}

    return stage5_failed


def make_stage5_cancelled_node(deps: ResearchOrchestrationDependencies):
    """stage5_cancelled：Stage5 child 人工取消 → orchestration cancelled。"""

    async def stage5_cancelled(state) -> dict:
        async with deps.sessionmaker() as session:
            await ResearchOrchestrationRepository(session).mark_cancelled(
                UUID(state["orchestration_id"]), datetime.now(UTC)
            )
            await session.commit()
        return {"current_phase": _phase_value(OrchestrationPhase.STAGE5)}

    return stage5_cancelled


def make_waiting_manual_node(deps: ResearchOrchestrationDependencies):
    """waiting_manual：fulfill 后仍 not ready → status=waiting_human、
    phase=waiting_manual（0 个 WorkflowRun，spec I Case 3）。"""

    async def waiting_manual(state) -> dict:
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.WAITING_HUMAN.value,
            _phase_value(OrchestrationPhase.WAITING_MANUAL),
        )
        return {"current_phase": _phase_value(OrchestrationPhase.WAITING_MANUAL)}

    return waiting_manual
