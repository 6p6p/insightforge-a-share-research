"""Stage 4 LangGraph state schema (spec G: checkpoint-safe small objects only).

只允许 checkpoint-safe 小对象：str / list[dict] / list[str] / str|None。
**不放过**：SQLAlchemy / Pydantic models / AsyncSession / LLM objects /
Evidence text / Calculation blobs / Macro pack / Comparison details /
prompt / reasoning_content / RawArtifact / Chroma。UUID 统一 string。
"""

from typing import Annotated, TypedDict


def merge_analysis_results(current, update):
    """Reducer：按 (item_id, analysis_type) 去重合并 worker 结果。

    - worker 并发写同一 channel（Send fan-out）需要 reducer（否则
      InvalidUpdateError）；
    - 去重保证 retry / resume 幂等：同一 item 重跑不产生重复结果；
    - 顺序无关：最终结果集合与 worker 完成顺序无关。
    """
    out = list(current or [])
    existing = {(r["item_id"], r["analysis_type"]) for r in out}
    for item in update or []:
        key = (item["item_id"], item["analysis_type"])
        if key not in existing:
            out.append(item)
            existing.add(key)
    return out


class Stage4WorkflowState(TypedDict, total=False):
    """分析工作流的一次执行状态（全部 checkpoint-safe）。"""

    company_id: str
    research_question: str
    analysis_as_of: str
    analysis_work_items: list[dict]
    analysis_results: Annotated[list[dict], merge_analysis_results]
    claim_ids: list[str]
    synthesis_id: str | None
    synthesis_result_id: str | None
