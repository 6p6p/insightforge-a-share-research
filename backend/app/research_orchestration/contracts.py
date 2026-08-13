"""Research orchestration contracts (stage 7A.2B.1 spec B/C/F).

顶层编排器 `research_orchestration_runs` **不是 WorkflowRun**：用独立的
`OrchestrationStatus` / `OrchestrationPhase` 枚举，不修改 `workflow_runs` 的
状态语义。orchestrator 身份（name/version）与 schema version 是**持久化**的
orchestrator identity，进入 input fingerprint（spec F）。
"""

import hashlib
import json
from enum import StrEnum
from uuid import UUID

# orchestrator 身份（persisted orchestrator_name / orchestrator_version /
# orchestration_schema_version）。进入 orchestration input fingerprint（spec F）。
ORCHESTRATION_SCHEMA_VERSION = 1
ORCHESTRATOR_NAME = "research_orchestrator"
ORCHESTRATOR_VERSION = 1

# 同 task 至多一个 active orchestration（partial unique index
# uq_research_orchestration_runs_one_active_per_task 的语义）。
ACTIVE_ORCHESTRATION_STATUSES = frozenset({"pending", "running", "waiting_human"})

# 补充研究最大轮数（7A.2B.3 spec K-X）：同一 orchestration 内最多执行
# MAX_BACKFLOW_RESEARCH_ROUNDS 轮 backflow（Stage4/5 child attempt 2、3）。
# 达到上限仍未 resolved → waiting_human / manual_required，稳定 reason
# research_backflow_limit_reached（防无限循环）。
MAX_BACKFLOW_RESEARCH_ROUNDS = 2

# backflow terminal 的稳定 reason（写进 checkpoint state + observability）。
RESEARCH_BACKFLOW_LIMIT_REACHED = "research_backflow_limit_reached"
RESEARCH_BACKFLOW_NO_PROGRESS = "research_backflow_no_progress"

# 7A Product Gate spec K2：可**受控补资料后同线程恢复**的 backflow manual reason
# 集合（对应 executor 级 `backflow_executor_manual_reasons`）：
#   - source_acquisition_required：缺 eligible source（用户补 PDF / approved URL
#     import 后可恢复，K1 走 prepare 重路由，K2 走同 round 重跑补充研究）。
# `structured_data_refresh_required` **不在此集合**（spec D2）：结构化
# financial/macro/valuation refresh 不在 automatic 文档补充研究范围（7A.2B.3
# scope 冻结），上传 PDF / URL 不能解决该缺口 → resume 以 InvalidAction 稳定拒绝，
# 不得伪装成 document retrieval 已解决。
# `research_backflow_limit_reached` **也不在此集合**（K3：不能绕过 MAX rounds）。
RESUME_BACKFLOW_MANUAL_REASONS = frozenset({"source_acquisition_required"})

# resume_after_source_acquisition 的 continuation kind（spec J/K）：
#   - "prepare"：waiting_manual → 从 ensure_route 后重跑 prepare（K1，补资料后
#     重路由，route_readiness 重新判定 → ready→Stage4 a1 | 仍缺→waiting_manual）；
#   - "supplemental_research"：research_backflow → 从 plan_supplemental_research
#     后继重跑 execute_supplemental_research（K2，同 research_request_id + 同
#     backflow_round，不 round+1、不新建 SupplementalPlan）。
RESUME_KIND_PREPARE = "prepare"
RESUME_KIND_SUPPLEMENTAL_RESEARCH = "supplemental_research"


class OrchestrationStatus(StrEnum):
    """一次 top-level orchestration 的状态（独立表，不改 workflow_runs 语义）。"""

    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class OrchestrationPhase(StrEnum):
    """当前阶段：planning → routing → preparing → (fulfilling → preparing) →
    stage4 → awaiting_stage5（7A.2B.1 正常 terminal phase；status 保持 running
    等 7A.2B.2 接 Stage5）→ … stage5 / research_backflow / completed（未来）。
    """

    PLANNING = "planning"
    ROUTING = "routing"
    PREPARING = "preparing"
    FULFILLING = "fulfilling"
    WAITING_MANUAL = "waiting_manual"
    STAGE4 = "stage4"
    AWAITING_STAGE5 = "awaiting_stage5"
    STAGE5 = "stage5"
    RESEARCH_BACKFLOW = "research_backflow"
    COMPLETED = "completed"


class ChildStage(StrEnum):
    """orchestration child stage（v1 仅 stage4；stage5 未来）。

    child lookup **必须精确** `(orchestration_id, stage, attempt_no)`（spec D），
    不得用 `latest task + graph_name` 猜归属。
    """

    STAGE4 = "stage4"
    STAGE5 = "stage5"


def compute_orchestration_input_fingerprint(
    *,
    orchestration_schema_version: int,
    task_id: UUID,
    planner_input_fingerprint: str,
    orchestrator_name: str,
    orchestrator_version: int,
) -> str:
    """orchestration input fingerprint（spec F）。

    = schema version + task_id + **planner input fingerprint**（同 task + 同 plan
    input + 同 orchestrator → replay 同一 orchestration）+ orchestrator 身份。
    canonical JSON SHA-256。**不含** API key / created_at / row identity。
    """
    payload = {
        "orchestration_schema_version": orchestration_schema_version,
        "task_id": str(task_id),
        "planner_input_fingerprint": planner_input_fingerprint,
        "orchestrator": {"name": orchestrator_name, "version": orchestrator_version},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
