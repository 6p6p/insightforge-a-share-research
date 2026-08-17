"""Canonical public task status projection (Product Consistency Hardening).

单一权威的「用户可见任务状态」推导：未开始 / 进行中 / 等待确认 / 已完成 / 失败。

背景（状态不一致 root cause）：`research_tasks.status` 创建后由顶层编排器独立
维护，且前端多个组件此前各自从不同数据源推导状态（task.status / workflow run
status / orchestration status / report 存在性）→ 同一任务出现「研究完成 /
待执行 / 已创建 / 未开始」并存。本模块是**唯一**的 public projection 来源：

- `public_status` 由 task 自身 + 最新 orchestration 推导，**不修改** LangGraph
  内部 state 语义、不修改 orchestration status/phase、不修改 research_tasks 表
  的 status 写路径；
- 推导优先级（高 → 低）：
  1. task.status ∈ {failed, cancelled} → `failed`（显式终态，不覆盖）；
  2. 最新 orchestration（active 优先）：
     - completed → `completed`（orchestration 只在 Stage5 finalize 后 complete，
       报告必已生成）；
     - failed / cancelled → `failed`；
     - waiting_human → `waiting_confirmation`（含 awaiting_stage5 人工裁决与
       waiting_manual / research_backflow 人工介入）；
     - pending / running → `in_progress`；
  3. 无 orchestration：task.status completed → `completed`（旧路径兼容）；
     running / retrying → `in_progress`；waiting_human → `waiting_confirmation`；
     pending → `not_started`。
"""

# 用户可见的五态（与前端 PUBLIC_STATUS_LABEL 对齐；不暴露内部枚举）。
PUBLIC_STATUS_NOT_STARTED = "not_started"  # 未开始
PUBLIC_STATUS_IN_PROGRESS = "in_progress"  # 进行中
PUBLIC_STATUS_WAITING_CONFIRMATION = "waiting_confirmation"  # 等待确认
PUBLIC_STATUS_COMPLETED = "completed"  # 已完成
PUBLIC_STATUS_FAILED = "failed"  # 失败

PUBLIC_STATUSES = (
    PUBLIC_STATUS_NOT_STARTED,
    PUBLIC_STATUS_IN_PROGRESS,
    PUBLIC_STATUS_WAITING_CONFIRMATION,
    PUBLIC_STATUS_COMPLETED,
    PUBLIC_STATUS_FAILED,
)

# orchestration 内部枚举（仅引用值，不修改语义）。
_ORCH_ACTIVE = frozenset({"pending", "running", "waiting_human"})
_ORCH_TERMINAL_FAILED = frozenset({"failed", "cancelled"})


def project_public_status(
    *,
    task_status: str,
    orchestration_status: str | None = None,
) -> str:
    """task + 最新 orchestration → canonical public status（纯函数，单一事实源）。

    `orchestration_status` 为 None 表示该 task 尚无任何 orchestration。
    """
    if task_status in ("failed", "cancelled"):
        return PUBLIC_STATUS_FAILED

    if orchestration_status is None:
        if task_status in ("completed",):
            return PUBLIC_STATUS_COMPLETED
        if task_status in ("running", "retrying"):
            return PUBLIC_STATUS_IN_PROGRESS
        if task_status in ("waiting_human",):
            return PUBLIC_STATUS_WAITING_CONFIRMATION
        return PUBLIC_STATUS_NOT_STARTED

    if orchestration_status == "completed":
        return PUBLIC_STATUS_COMPLETED
    if orchestration_status in _ORCH_TERMINAL_FAILED:
        return PUBLIC_STATUS_FAILED
    if orchestration_status in _ORCH_ACTIVE:
        if orchestration_status == "waiting_human":
            return PUBLIC_STATUS_WAITING_CONFIRMATION
        return PUBLIC_STATUS_IN_PROGRESS
    # 未知 orchestration 枚举（防御）：回退 task 自身语义。
    if task_status == "completed":
        return PUBLIC_STATUS_COMPLETED
    if task_status in ("failed", "cancelled"):
        return PUBLIC_STATUS_FAILED
    return PUBLIC_STATUS_IN_PROGRESS
