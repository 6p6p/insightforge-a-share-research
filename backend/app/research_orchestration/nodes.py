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

from app.domain.tasks import WorkflowRunStatus
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.errors import ResearchOrchestrationIntegrityError
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.stage5.contracts import (
    STAGE5_TERMINAL_CANCELLED,
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
STAGE5_ROUTE_WAITING_HUMAN = "waiting_human"
STAGE5_ROUTE_RESEARCH_REQUIRED = "research_required"
STAGE5_ROUTE_FAILED = "failed"
STAGE5_ROUTE_CANCELLED = "cancelled"
_STAGE5_ROUTE_VALUES = frozenset(
    {
        STAGE5_ROUTE_COMPLETED,
        STAGE5_ROUTE_WAITING_HUMAN,
        STAGE5_ROUTE_RESEARCH_REQUIRED,
        STAGE5_ROUTE_FAILED,
        STAGE5_ROUTE_CANCELLED,
    }
)


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
    return STAGE5_ROUTE_COMPLETED, {}


def make_ensure_stage5_child_node(deps: ResearchOrchestrationDependencies):
    """ensure_stage5_child：exact child (orchestration_id, stage5, attempt 1)。

    - 从真实 Stage4 checkpoint 投影 `Stage5WorkflowRequest`（spec J/K），不复制
      Stage4→Stage5 bridge 逻辑；
    - 经 `ResearchOrchestrationChildService.ensure_stage5_child` **同一事务**创建
      WorkflowRun + child link（已有 child → attach，不重复 create，spec K）。
    """

    async def ensure_stage5_child(state) -> dict:
        stage5_request = await _stage5_request(deps, state)
        child = await deps.child_service.ensure_stage5_child(
            UUID(state["orchestration_id"]), stage5_request
        )
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.STAGE5),
        )
        return {
            "current_child_run_id": str(child.run_id),
            "current_phase": _phase_value(OrchestrationPhase.STAGE5),
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
        stage5_request = await _stage5_request(deps, state)
        async with deps.sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None:
            raise ResearchOrchestrationIntegrityError("orchestration child run missing")
        if run.status == WorkflowRunStatus.PENDING.value:
            await deps.stage5_runner.execute_stage5(run_id, stage5_request)
        elif run.status == WorkflowRunStatus.FAILED.value:
            # request 用于"crash 在 execute_stage5 前"的 run（无 checkpoint）：
            # resume 方法复用同 run/thread 重新首启。
            await deps.stage5_runner.resume_stage5_for_recovery(run_id, request=stage5_request)
        # completed / waiting_human / cancelled / running：不重复执行。

        status, extra = await _stage5_outcome(deps, run_id)
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.STAGE5),
        )
        return {
            "stage5_run_status": status,
            "current_phase": _phase_value(OrchestrationPhase.STAGE5),
            **extra,
        }

    return run_or_resume_stage5


def route_stage5_result(state) -> str:
    """run_or_resume_stage5 → 条件边（**纯 state 路由**，不碰 DB，spec L/M）。

    continuation（spec M）：runner 用 `aupdate_state(config, {"current_phase":
    stage5}, as_node="ensure_stage5_child")` 重新进入 `run_or_resume_stage5`
    后，节点重新投影 fresh `stage5_run_status`，本函数据此重新判定。
    """
    status = state.get("stage5_run_status")
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


def make_pause_for_research_node(deps: ResearchOrchestrationDependencies):
    """pause_for_research：Stage5 research_required → **只持久化 research_request_id
    + phase=research_backflow**（spec P；不实现 backflow 循环）。"""

    async def pause_for_research(state) -> dict:
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.WAITING_HUMAN.value,
            _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        )
        return {
            "research_request_id": state.get("research_request_id"),
            "current_phase": _phase_value(OrchestrationPhase.RESEARCH_BACKFLOW),
        }

    return pause_for_research


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
