"""Top-level research orchestration graph nodes (stage 7A.2B.1 spec I/J).

节点**复用**现有 services（ResearchPlanningService / ResearchSourceRouter /
ResearchPreparationService / ResearchFulfillmentService / Stage4WorkflowRunner /
SynthesisService），不复制业务逻辑（spec J）。每个节点是幂等的：graph 用
checkpointer 重放 / resume 时，节点按需精确读取，不重复 create（plan / route /
prepare replay；Stage4 child 经 exact `get_child` attach/existing，spec D）。

- `_persist_phase`：短事务把 orchestration status + current_phase 投影到
  `research_orchestration_runs`（observability；graph 期间不持有 session）。
- `run_or_resume_stage4`：按 child WorkflowRun 状态决定 execute（pending）/
  resume（failed(worker_restarted)）/ 跳过（completed / running，后者由恢复
  协调器处理）——**不重复 create Stage4 run**。
- Stage4 内部失败（execute/resume 抛）由节点向上传播，顶层 runner 投影为
  orchestration failed（不吞 child 错误，spec M）。
"""

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
        return {
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
    """

    async def collect_synthesis(state) -> dict:
        run_id = UUID(state["current_child_run_id"])
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
            _phase_value(OrchestrationPhase.AWAITING_STAGE5),
        )
        return {
            "synthesis_result_id": str(synthesis_result_id),
            "current_phase": _phase_value(OrchestrationPhase.AWAITING_STAGE5),
        }

    return collect_synthesis


# ------------------------------------------------------------------ terminals


def make_awaiting_stage5_node(deps: ResearchOrchestrationDependencies):
    """awaiting_stage5：7A.2B.1 正常 terminal phase（status 保持 running，
    等 7A.2B.2 接 Stage5）；幂等重放。"""

    async def awaiting_stage5(state) -> dict:
        await _persist_phase(
            deps.sessionmaker,
            state["orchestration_id"],
            OrchestrationStatus.RUNNING.value,
            _phase_value(OrchestrationPhase.AWAITING_STAGE5),
        )
        return {"current_phase": _phase_value(OrchestrationPhase.AWAITING_STAGE5)}

    return awaiting_stage5


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
