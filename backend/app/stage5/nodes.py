"""Stage 5 report control workflow nodes (thin orchestration; business logic in services).

Role boundaries (spec D/O/Q/R/S):
- nodes 只协调既有 Application Services，不重写业务逻辑；
- 确定性任务交给代码：Outline → DraftSections → Report → Check → Audit →
  ReviewAction 全走 Services（fingerprint / replay 幂等，retry / resume 不产生
  重复业务对象）；
- 修订环（spec O）：`route_action` 判定 rewrite 且 `revision_round > MAX` →
  terminal `revision_limit_exceeded`（不再重写）；否则 `rewrite_sections` 逐个
  修订 target section → 回 `assemble_report`（**新 Report**，spec N）；
- 人审（spec Q）：`wait_human` 用真实 LangGraph `interrupt()`；resume 值
  = `{human_decision_id, decision, comment}`（decision 由 API 层先经
  `resolve_human_request` 持久化）；
- approve 安全（spec R）：`finalize_on_approve` 只 finalize 当前 Report 且
  前提 deterministic Check=pass，否则 `Stage5ApproveRequiresPassCheck`；
- research 本轮不执行（spec S）：route=research / human 决定 research →
  terminal `research_required`，不假装 research completed。
"""

from datetime import date
from uuid import UUID

from langgraph.types import interrupt

from app.audit.contracts import ReportAuditRequest
from app.draft_section.contracts import DraftSectionRequest
from app.draft_section.errors import DraftSectionModelUnavailable
from app.report.contracts import CHECK_STATUS_PASS, ReportAssemblyDraft
from app.review.contracts import (
    ACTION_TYPE_FINALIZE,
    ACTION_TYPE_HUMAN_REVIEW,
    ACTION_TYPE_RESEARCH,
    ACTION_TYPE_REWRITE,
    HUMAN_DECISION_APPROVE,
    HUMAN_DECISION_CANCEL,
    HUMAN_DECISION_RESEARCH,
    HUMAN_DECISION_REWRITE,
    HUMAN_DECISIONS,
)
from app.revision.contracts import (
    TRIGGER_TYPE_AUDIT_REWRITE,
    TRIGGER_TYPE_HUMAN_REWRITE,
    RevisionRequest,
    RevisionTrigger,
)
from app.revision.derive import action_target_section_ids
from app.stage5.contracts import (
    MAX_STAGE5_REVISION_ROUNDS,
    STAGE5_TERMINAL_CANCELLED,
    STAGE5_TERMINAL_FINALIZE,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
    STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED,
)
from app.stage5.dependencies import Stage5WorkflowDependencies
from app.stage5.errors import (
    Stage5ApproveRequiresPassCheck,
    Stage5InvalidHumanResume,
    Stage5InvalidState,
)


def make_validate_stage5_request_node():
    """validate_stage5_request：结构性校验初始 state（不重做业务规则）。

    请求构造（Stage5WorkflowRequest）已做完整校验；此处只防御性确认 graph
    state 形状：上下文字段齐备、analysis_as_of 是 ISO date、UUID 字段合法。
    任一失败 → Stage5InvalidState（稳定错误码）。
    """

    async def validate_stage5_request(state) -> dict:
        for key in (
            "task_id",
            "company_id",
            "research_question",
            "analysis_as_of",
            "synthesis_result_id",
        ):
            value = state.get(key)
            if not isinstance(value, str) or not value:
                raise Stage5InvalidState(f"{key} 缺失")
        try:
            date.fromisoformat(state["analysis_as_of"])
        except (KeyError, TypeError, ValueError):
            raise Stage5InvalidState("analysis_as_of 必须是 ISO date") from None
        try:
            UUID(state["company_id"])
            UUID(state["synthesis_result_id"])
        except (KeyError, TypeError, ValueError):
            raise Stage5InvalidState("company_id / synthesis_result_id 必须是 UUID") from None
        return {}

    return validate_stage5_request


def make_build_report_draft_node(deps: Stage5WorkflowDependencies):
    """build_report_draft：SynthesisResult → Outline（5A）+ 全部 sections（5B）。

    只跑一次（round 0）。后续修订轮次不重建整份 draft——只替换被修订 section 的
    draft id（`rewrite_sections`）。
    """

    async def build_report_draft(state) -> dict:
        synthesis_result_id = state.get("synthesis_result_id")
        if not synthesis_result_id:
            raise Stage5InvalidState("build_report_draft 需要 synthesis_result_id")
        outline = await deps.report_outline_service.create_or_get_outline(UUID(synthesis_result_id))
        verified_outline = await deps.report_outline_service.verify_outline_integrity(
            outline.outline_id
        )
        sections: list[dict] = []
        degraded_count = 0
        for section in verified_outline.sections:
            try:
                result = await deps.draft_section_service.create_or_get_section(
                    DraftSectionRequest(
                        outline_id=verified_outline.outline_id, section_id=section.section_id
                    )
                )
                sections.append(
                    {
                        "section_id": section.section_id,
                        "section_order": section.section_order,
                        "section_type": section.section_type,
                        "title": section.title,
                        "draft_section_id": str(result.draft_section_id),
                        "section_status": "completed",
                    }
                )
            except DraftSectionModelUnavailable:
                degraded_count += 1
                sections.append(
                    {
                        "section_id": section.section_id,
                        "section_order": section.section_order,
                        "section_type": section.section_type,
                        "title": section.title,
                        "draft_section_id": None,
                        "section_status": "degraded",
                        "degraded_reason": "model_unavailable",
                    }
                )
        return {
            "outline_id": str(verified_outline.outline_id),
            "sections": sections,
            "degraded_section_count": degraded_count,
            "section_count": len(verified_outline.sections),
        }

    return build_report_draft


def make_assemble_report_node(deps: Stage5WorkflowDependencies):
    """assemble_report：当前 sections（含修订输出）→ **新** Report（spec N）。

    每轮修订后都走这里装配新 Report（不 UPDATE 旧 Report）；修订 draft 的
    writer_input_fingerprint 不同 → 新 report_fingerprint → 新行。
    """

    async def assemble_report(state) -> dict:
        outline_id = state.get("outline_id")
        sections = state.get("sections")
        if not outline_id or not sections:
            raise Stage5InvalidState("assemble_report 需要 outline_id + sections")
        ordered = sorted(sections, key=lambda s: s.get("section_order", 0))
        valid = [s for s in ordered if s.get("draft_section_id") is not None]
        degraded = [s for s in ordered if s.get("section_status") == "degraded"]
        if not valid:
            raise Stage5InvalidState(f"所有 {len(ordered)} 个 section 均 degraded，无法装配报告")
        draft_section_ids = tuple(UUID(s["draft_section_id"]) for s in valid)
        result = await deps.report_service.create_or_get_report(
            ReportAssemblyDraft(outline_id=UUID(outline_id), draft_section_ids=draft_section_ids)
        )
        return {
            "report_id": str(result.report_id),
            "assembled_section_count": len(draft_section_ids),
            "degraded_section_count": len(degraded),
        }

    return assemble_report


def make_check_report_node(deps: Stage5WorkflowDependencies):
    """check_report：确定性 10 项 v1 checks（5C，0 LLM）。"""

    async def check_report(state) -> dict:
        report_id = state.get("report_id")
        if not report_id:
            raise Stage5InvalidState("check_report 需要 report_id")
        result = await deps.report_check_service.run_report_checks(UUID(report_id))
        return {"check_result_id": str(result.check_result_id)}

    return check_report


def make_audit_report_node(deps: Stage5WorkflowDependencies):
    """audit_report：verified Report + verified CheckResult → Agent Audit（5D）。"""

    async def audit_report(state) -> dict:
        report_id = state.get("report_id")
        check_result_id = state.get("check_result_id")
        if not report_id or not check_result_id:
            raise Stage5InvalidState("audit_report 需要 report_id + check_result_id")
        result = await deps.report_audit_service.create_or_get_audit(
            ReportAuditRequest(report_id=UUID(report_id), check_result_id=UUID(check_result_id))
        )
        return {"audit_id": str(result.audit_id)}

    return audit_report


def make_route_action_node(deps: Stage5WorkflowDependencies):
    """route_action：ReviewActionService 派生 action → 分支。

    - finalize → terminal `finalize`（Gate 0 已在 derive_action_type 强制）；
    - research → terminal `research_required`（spec S，本轮不执行 research）；
    - rewrite → 超限（`revision_round > MAX`）→ terminal `revision_limit_exceeded`
      （spec O，WorkflowRun FAILED）；否则设 audit_rewrite trigger → 进修订环；
    - human_review → 创建 human request（persist）→ 设人审上下文 → interrupt。
    """

    async def route_action(state) -> dict:
        audit_id = state.get("audit_id")
        if not audit_id:
            raise Stage5InvalidState("route_action 需要 audit_id")
        action = await deps.review_action_service.create_or_get_action(UUID(audit_id))
        action_type = action.action_type

        update: dict = {
            "review_action_id": str(action.review_action_id),
            "route": action_type,
        }
        if action_type in (ACTION_TYPE_REWRITE, ACTION_TYPE_HUMAN_REVIEW):
            update["revision_target_section_ids"] = list(
                action_target_section_ids(action.action_payload)
            )

        if action_type == ACTION_TYPE_REWRITE:
            if state.get("revision_round", 1) > MAX_STAGE5_REVISION_ROUNDS:
                update["terminal"] = STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED
            else:
                update["revision_trigger_type"] = TRIGGER_TYPE_AUDIT_REWRITE
                update["revision_trigger_artifact_id"] = str(action.review_action_id)
        elif action_type == ACTION_TYPE_HUMAN_REVIEW:
            request = await deps.review_action_service.create_or_get_human_request(
                action.review_action_id
            )
            update["human_request_id"] = str(request.human_request_id)
        elif action_type == ACTION_TYPE_FINALIZE:
            update["terminal"] = STAGE5_TERMINAL_FINALIZE
        elif action_type == ACTION_TYPE_RESEARCH:
            update["terminal"] = STAGE5_TERMINAL_RESEARCH_REQUIRED
        else:
            raise Stage5InvalidState(f"未知 action_type: {action_type!r}")
        return update

    return route_action


def make_rewrite_sections_node(deps: Stage5WorkflowDependencies):
    """rewrite_sections：逐个修订 target section → 更新 sections / 计数。

    - 只修订 `revision_target_section_ids` 覆盖的 section；每个走
      `RevisionService.revise_section`（source draft + trigger + round）；
    - trigger 来自 state（audit_rewrite=review_action_id；human_rewrite=
      human_decision_id），与 RevisionTrigger 三选一强约束一致；
    - 修订完成后重置下游 IDs（report/check/audit/action/route/human）→ 回
      `assemble_report` 装配新 Report（spec N bounded loop）。
    """

    async def rewrite_sections(state) -> dict:
        target_ids = state.get("revision_target_section_ids")
        if not isinstance(target_ids, list) or not target_ids:
            raise Stage5InvalidState("rewrite_sections 需要 target_section_ids")
        trigger_type = state.get("revision_trigger_type")
        artifact_id = state.get("revision_trigger_artifact_id")
        if trigger_type not in (TRIGGER_TYPE_AUDIT_REWRITE, TRIGGER_TYPE_HUMAN_REWRITE):
            raise Stage5InvalidState("rewrite_sections 需要有效的 trigger_type")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise Stage5InvalidState("rewrite_sections 需要 trigger artifact id")

        if trigger_type == TRIGGER_TYPE_AUDIT_REWRITE:
            trigger = RevisionTrigger(review_action_id=UUID(artifact_id))
        else:
            trigger = RevisionTrigger(human_decision_id=UUID(artifact_id))

        revision_round = state.get("revision_round", 1)
        drafts_by_section = {
            s["section_id"]: s["draft_section_id"]
            for s in state.get("sections", [])
            if isinstance(s.get("section_id"), str) and isinstance(s.get("draft_section_id"), str)
        }
        missing = [sid for sid in target_ids if sid not in drafts_by_section]
        if missing:
            raise Stage5InvalidState("rewrite 目标 section 不在当前 sections 中")

        revisions: list[dict] = []
        for section_id in target_ids:
            result = await deps.revision_service.revise_section(
                RevisionRequest(
                    source_draft_section_id=UUID(drafts_by_section[section_id]),
                    trigger=trigger,
                    revision_round=revision_round,
                )
            )
            revisions.append(
                {
                    "revision_id": str(result.revision_id),
                    "section_id": section_id,
                    "source_draft_section_id": drafts_by_section[section_id],
                    "revised_draft_section_id": str(result.revised_draft_section_id),
                    "revision_round": revision_round,
                    "trigger_type": trigger_type,
                }
            )

        revised_by_section = {r["section_id"]: r["revised_draft_section_id"] for r in revisions}
        updated_sections = [
            {**s, "draft_section_id": revised_by_section[s["section_id"]]}
            if s["section_id"] in revised_by_section
            else s
            for s in state.get("sections", [])
        ]

        return {
            "sections": updated_sections,
            "revisions": revisions,
            "revision_round": revision_round + 1,
            "report_id": None,
            "check_result_id": None,
            "audit_id": None,
            "review_action_id": None,
            "route": None,
            "revision_target_section_ids": [],
            "revision_trigger_type": None,
            "revision_trigger_artifact_id": None,
            "human_request_id": None,
            "human_decision_id": None,
            "human_decision": None,
            "human_comment": None,
        }

    return rewrite_sections


def make_wait_human_node():
    """wait_human：真实 LangGraph `interrupt()`（spec Q，WAITING_HUMAN semantics）。

    首次执行抛 GraphInterrupt → 暂停；runner 检测到 interrupt → run 置
    WAITING_HUMAN。resume 值 = `{human_decision_id, decision, comment}`（decision
    已由 API 层经 `resolve_human_request` 持久化为 immutable artifact）。

    decision 分支（spec R/S）：
    - approve → `finalize_on_approve`（校验 Check=pass 后才 finalize）；
    - rewrite → `rewrite_sections`（human_rewrite trigger）；
    - research / cancel → terminal（research_required / cancelled）。
    """

    async def wait_human(state) -> dict:
        human_request_id = state.get("human_request_id")
        if not human_request_id:
            raise Stage5InvalidState("wait_human 需要 human_request_id")
        resume = interrupt(
            {
                "interrupt_key": "human_review",
                "human_request_id": human_request_id,
                "message": "请人工裁决：approve / rewrite / research / cancel",
            }
        )
        if not isinstance(resume, dict):
            raise Stage5InvalidHumanResume("resume payload 必须是 dict")
        decision = resume.get("decision")
        if decision not in HUMAN_DECISIONS:
            raise Stage5InvalidHumanResume("decision 必须是 approve/rewrite/research/cancel")
        human_decision_id = resume.get("human_decision_id")
        if not isinstance(human_decision_id, str) or not human_decision_id:
            raise Stage5InvalidHumanResume("human_decision_id 缺失")

        update: dict = {
            "human_decision_id": human_decision_id,
            "human_decision": decision,
            "human_comment": resume.get("comment"),
        }
        if decision == HUMAN_DECISION_REWRITE:
            update["revision_trigger_type"] = TRIGGER_TYPE_HUMAN_REWRITE
            update["revision_trigger_artifact_id"] = human_decision_id
        elif decision == HUMAN_DECISION_RESEARCH:
            update["terminal"] = STAGE5_TERMINAL_RESEARCH_REQUIRED
        elif decision == HUMAN_DECISION_CANCEL:
            update["terminal"] = STAGE5_TERMINAL_CANCELLED
        # approve：terminal 由 finalize_on_approve 设置（需先校验 Check=pass）。
        return update

    return wait_human


def make_finalize_on_approve_node(deps: Stage5WorkflowDependencies):
    """finalize_on_approve：spec R——approve 只能 finalize 当前 Report 且前提
    deterministic Check=pass（Gate 0 不被人工裁决覆盖）。

    Check=fail → `Stage5ApproveRequiresPassCheck`（run FAILED，不静默改道）。
    """

    async def finalize_on_approve(state) -> dict:
        check_result_id = state.get("check_result_id")
        if not check_result_id:
            raise Stage5InvalidState("finalize_on_approve 需要 check_result_id")
        verified = await deps.report_check_service.verify_check_result_integrity(
            UUID(check_result_id)
        )
        if verified.status != CHECK_STATUS_PASS:
            raise Stage5ApproveRequiresPassCheck()
        return {"terminal": STAGE5_TERMINAL_FINALIZE}

    return finalize_on_approve


def make_create_research_backflow_request_node(deps: Stage5WorkflowDependencies):
    """create_research_backflow_request：research_required terminal → 持久化可验证
    research 交接请求（spec Q，5E.2B）。

    只由 route_action（research action）或 wait_human（human decision=research）
    路由到本节点；节点调 `ResearchBackflowService.create_or_get_request`（幂等
    create_or_get + replay，0 LLM / 0 检索 / 0 Chroma）。terminal 不是
    research_required / 非法 trigger / 状态缺失 → run FAILED，不静默改道。
    """

    async def create_research_backflow_request(state) -> dict:
        source_run_id = state.get("source_stage5_run_id")
        if not source_run_id:
            raise Stage5InvalidState("create_research_backflow_request 需要 source_stage5_run_id")
        result = await deps.research_backflow_service.create_or_get_request(UUID(source_run_id))
        return {"research_request_id": str(result.research_request_id)}

    return create_research_backflow_request


# ------------------------------------------------------------------ conditional edges


def route_after_action(state) -> str:
    """route_action 后的分支：research_required → create_research_backflow_request；
    其他 terminal（finalize / revision_limit_exceeded）→ END；rewrite /
    human_review → 对应节点。"""
    terminal = state.get("terminal")
    if terminal is not None:
        if terminal == STAGE5_TERMINAL_RESEARCH_REQUIRED:
            return "create_research_backflow_request"
        return "END"
    route = state.get("route")
    if route == ACTION_TYPE_REWRITE:
        return "rewrite_sections"
    if route == ACTION_TYPE_HUMAN_REVIEW:
        return "wait_human"
    return "END"


def route_after_human(state) -> str:
    """wait_human resume 后的分支：approve → finalize_on_approve；rewrite →
    rewrite_sections；research → create_research_backflow_request（terminal 已由
    wait_human 设置）；cancel → END。"""
    decision = state.get("human_decision")
    if decision == HUMAN_DECISION_REWRITE:
        return "rewrite_sections"
    if decision == HUMAN_DECISION_APPROVE:
        return "finalize_on_approve"
    if decision == HUMAN_DECISION_RESEARCH:
        return "create_research_backflow_request"
    return "END"
