"""Top-level research orchestration LangGraph state (stage 7A.2B.1 spec H).

只允许 checkpoint-safe 小对象：str / bool / list[str] / str|None。**不放过**：
report / evidence body / claims / raw / prompt / reasoning / API key /
SQLAlchemy / Pydantic models / AsyncSession。UUID 统一 string。

State 只存 ID 与当前阶段——真正的数据在 `research_orchestration_runs` /
`research_plan` / child `workflow_runs` checkpoint / `SynthesisResult`，
节点按需从 repository / service 精确读取。
"""

from typing import TypedDict


class ResearchOrchestrationState(TypedDict, total=False):
    """一次 top-level orchestration 的执行状态（全部 checkpoint-safe）。"""

    orchestration_id: str
    task_id: str
    research_plan_id: str
    current_phase: str
    # prepare 结果摘要（不存 missing need 正文 / stage4 request 正文）。
    preparation_ready: bool
    missing_need_codes: list[str]
    # stage4 child run（exact ownership，spec D）→ run_id；immutable 锚点：
    # ensure_stage5_child / run_or_resume_stage5 从这里重建 Stage5 request
    # （Stage5RequestBuilder.from_stage4_state，spec J）。
    stage4_child_run_id: str
    # current child run（ensure_stage4_child → stage4 id；ensure_stage5_child 起
    # 变为 stage5 id）。
    current_child_run_id: str
    # collect_synthesis 之后：真实 Stage4 checkpoint 的 synthesis_result_id。
    synthesis_result_id: str
    # run_or_resume_stage5 投影的 Stage5 child 终态（route_stage5_result 只读
    # state，不碰 DB——续接 aupdate_state 注入 fresh 值后条件边重新判定）。
    stage5_run_status: str
    # research_required terminal 时：Stage5 checkpoint 的 research_request_id
    # （backflow loop 的 input 唯一入口；Stage5 每轮 research_required 创建新
    # request，loop 用最新的，spec 7A.2B.3）。
    research_request_id: str
    # backflow loop（7A.2B.3）：当前已完成补充研究轮数（0 = 未进入 loop；
    # route_stage5_result 在 research_required 且 round < MAX 时进入 loop）。
    backflow_round: int
    # plan_supplemental_research 之后：create_or_get_plan 的确定性补充计划 id。
    backflow_plan_id: str
    # execute_supplemental_research 投影的**新增** relevant EvidenceCard id
    # （canonical 排序；verify_progress 据此判定 progress，prepare_updated_analysis
    # 据此组装新 Stage4 输入——v1 只判 EvidenceCard）。
    backflow_new_evidence_card_ids: list[str]
    # execute_supplemental_research 投影的 plan 级 manual_required_reasons（7A.2B.3
    # scope 冻结：structured financial/macro/valuation refresh 不在 automatic 文档
    # 补充研究范围 → verify_progress 给稳定 reason structured_data_refresh_required，
    # **不误报 research_backflow_no_progress**）。
    backflow_manual_reasons: list[str]
    # execute_supplemental_research 投影的 executor 级 manual_required_reasons
    # （7A Product Gate spec I/J：per-need 缺 eligible source → 聚合
    # source_acquisition_required，结构化缺数据 → structured_data_refresh_required）。
    # 与 plan 级 `backflow_manual_reasons` 分开——verify_progress 理由优先级：
    # plan reasons > executor reasons > research_backflow_no_progress。
    backflow_executor_manual_reasons: list[str]
    # verify_progress 结果：True → 有进度（进入 Stage4 attempt N+1）；False →
    # manual_required（reason=research_backflow_no_progress）。
    backflow_progress: bool
    # backflow terminal 的稳定 reason（research_backflow_limit_reached /
    # research_backflow_no_progress；checkpoint state observability）。
    backflow_manual_reason: str
    # fulfill_request 之后：consumed 的新 SynthesisResult 的 fulfillment 行 id
    # （build_stage5_continuation_request 据此重建 Stage5 attempt 的 request）。
    fulfillment_id: str
    # 失败时 runner 投影的稳定 error_code。
    error_code: str
