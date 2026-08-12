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
    # stage4 child run（exact ownership，spec D）→ run_id。
    current_child_run_id: str
    # collect_synthesis 之后：真实 Stage4 checkpoint 的 synthesis_result_id。
    synthesis_result_id: str
    # 失败时 runner 投影的稳定 error_code。
    error_code: str
