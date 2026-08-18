"""Stage 5 report control workflow state schema (spec D/O: checkpoint-safe small objects only).

只允许 checkpoint-safe 小对象：str / list[dict] / list[str] / int / str|None。
**不放过**：SQLAlchemy / Pydantic models / AsyncSession / LLM objects /
Evidence text / Calculation blobs / prompt / reasoning_content / RawArtifact /
Chroma。UUID 统一 string；`sections` 只存 section 身份 + 当前 draft id，正文在
DB（service 层按需加载）。

全部业务 IDs / 派生状态由节点写回；graph 执行期间不持有 DB session。
"""

from typing import Annotated, TypedDict


def merge_sections(current, update):
    """Reducer：按 section_id 去重合并（rewrite 替换该 section 的当前 draft id）。

    - rewrite 节点返回整份 sections 列表，其中被修订 section 的 draft_section_id
      已替换为新修订 draft——同 section 出现两次时**后者覆盖前者**；
    - 未修订 section 原样保留（retry / resume 幂等：不产生重复 entry）。
    """
    merged: dict[str, dict] = {}
    for item in list(current or []) + list(update or []):
        if isinstance(item, dict) and isinstance(item.get("section_id"), str):
            merged[item["section_id"]] = item
    return list(merged.values())


def merge_revisions(current, update):
    """Reducer：追加 revisions，按 revision_id 去重（retry / resume 幂等）。"""
    out = list(current or [])
    seen = {item["revision_id"] for item in out if item.get("revision_id")}
    for item in update or []:
        if isinstance(item, dict) and item.get("revision_id") not in seen:
            out.append(item)
            seen.add(item["revision_id"])
    return out


class Stage5WorkflowState(TypedDict, total=False):
    """Stage 5 报告控制流一次执行的状态（全部 checkpoint-safe）。"""

    # 请求上下文（初始 state 注入）。
    task_id: str
    company_id: str
    research_question: str
    analysis_as_of: str
    synthesis_result_id: str
    # 当前 Stage 5 WorkflowRun（research_required terminal 时用于创建 research
    # backflow request；由 runner 在首次执行时注入）。
    source_stage5_run_id: str | None

    # 报告装配（5A Outline → 5B sections → 5C Report）。
    outline_id: str | None
    sections: Annotated[list[dict], merge_sections]
    section_count: int | None
    degraded_section_count: int | None
    assembled_section_count: int | None
    report_id: str | None

    # 确定性检查 + 审计 + 路由。
    check_result_id: str | None
    audit_id: str | None
    review_action_id: str | None
    route: str | None

    # 修订环（spec O bounded loop）。
    revision_round: int
    revision_target_section_ids: list[str]
    revision_trigger_type: str | None
    revision_trigger_artifact_id: str | None
    revisions: Annotated[list[dict], merge_revisions]

    # 人审（spec Q：真实 interrupt + WAITING_HUMAN semantics）。
    human_request_id: str | None
    human_decision_id: str | None
    human_decision: str | None
    human_comment: str | None

    # 终态投影（runner 映射为 run status）。
    terminal: str | None
    # research_required 终态必带：research backflow 交接请求 id（5E.2B）。
    research_request_id: str | None
