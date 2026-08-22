"""Canonical public task status projection (Product Consistency Hardening).

单一权威的「用户可见任务状态」推导：未开始 / 进行中 / 等待确认 / 已完成 / 失败 / 已取消。

背景（状态不一致 root cause）：`research_tasks.status` 创建后由顶层编排器独立
维护，且前端多个组件此前各自从不同数据源推导状态（task.status / workflow run
status / orchestration status / report 存在性）→ 同一任务出现「研究完成 /
待执行 / 已创建 / 未开始」并存。本模块是**唯一**的 public projection 来源：

- `public_status` 由 task 自身 + 最新 orchestration 推导，**不修改** LangGraph
  内部 state 语义、不修改 orchestration status/phase、不修改 research_tasks 表
  的 status 写路径；
- v1.2.6：`completed_with_warnings`（人工接受带审核提醒的报告）是 **terminal
  completed** 状态 → 投影为 `completed`（已完成），绝不落入 `in_progress`
  进行中 fallback；真实 DB 状态保留，展示区分由
  `project_completed_with_warnings` 提供（presentation only）；
- 推导优先级（高 → 低）：
  1. task.status 显式终态（不覆盖）：
     - cancelled → `cancelled`（P3.2 取消独立成态）；failed → `failed`；
  2. 最新 orchestration（active 优先）：
     - completed / completed_with_warnings → `completed`（orchestration 只在
       Stage5 finalize 后 complete，报告必已生成）；
     - cancelled → `cancelled`；failed → `failed`；
     - waiting_human → `waiting_confirmation`（含 awaiting_stage5 人工裁决与
       waiting_manual / research_backflow 人工介入）；
     - pending / running → `in_progress`；
  3. 无 orchestration：task.status completed → `completed`（旧路径兼容）；
     running / retrying → `in_progress`；waiting_human → `waiting_confirmation`；
     pending → `not_started`。
"""

# 用户可见的六态（与前端 PUBLIC_STATUS_LABEL 对齐；不暴露内部枚举）。
PUBLIC_STATUS_NOT_STARTED = "not_started"  # 未开始
PUBLIC_STATUS_IN_PROGRESS = "in_progress"  # 进行中
PUBLIC_STATUS_WAITING_CONFIRMATION = "waiting_confirmation"  # 等待确认
PUBLIC_STATUS_COMPLETED = "completed"  # 已完成
PUBLIC_STATUS_FAILED = "failed"  # 失败
PUBLIC_STATUS_CANCELLED = "cancelled"  # 已取消（P3.2 独立成态）

PUBLIC_STATUSES = (
    PUBLIC_STATUS_NOT_STARTED,
    PUBLIC_STATUS_IN_PROGRESS,
    PUBLIC_STATUS_WAITING_CONFIRMATION,
    PUBLIC_STATUS_COMPLETED,
    PUBLIC_STATUS_FAILED,
    PUBLIC_STATUS_CANCELLED,
)

# orchestration 内部枚举（仅引用值，不修改语义）。
_ORCH_ACTIVE = frozenset({"pending", "running", "waiting_human"})


def project_public_status(
    *,
    task_status: str,
    orchestration_status: str | None = None,
) -> str:
    """task + 最新 orchestration → canonical public status（纯函数，单一事实源）。

    `orchestration_status` 为 None 表示该 task 尚无任何 orchestration。

    v1.2.6：`completed_with_warnings` 是 terminal completed —— 投影为
    `PUBLIC_STATUS_COMPLETED`（已完成），禁止落入 `in_progress` fallback；
    普通「已完成」与「已完成（包含审核提醒）」由 `project_completed_with_warnings`
    区分（presentation layer，不改 DB 真实状态、不转成 completed）。
    """
    if task_status == "cancelled":
        return PUBLIC_STATUS_CANCELLED
    if task_status == "failed":
        return PUBLIC_STATUS_FAILED

    if orchestration_status is None:
        if task_status in ("completed", "completed_with_warnings"):
            return PUBLIC_STATUS_COMPLETED
        if task_status in ("running", "retrying"):
            return PUBLIC_STATUS_IN_PROGRESS
        if task_status in ("waiting_human",):
            return PUBLIC_STATUS_WAITING_CONFIRMATION
        return PUBLIC_STATUS_NOT_STARTED

    if orchestration_status in ("completed", "completed_with_warnings"):
        return PUBLIC_STATUS_COMPLETED
    if orchestration_status == "cancelled":
        return PUBLIC_STATUS_CANCELLED
    if orchestration_status == "failed":
        return PUBLIC_STATUS_FAILED
    if orchestration_status in _ORCH_ACTIVE:
        if orchestration_status == "waiting_human":
            return PUBLIC_STATUS_WAITING_CONFIRMATION
        return PUBLIC_STATUS_IN_PROGRESS
    # 未知 orchestration 枚举（防御）：回退 task 自身语义。
    if task_status in ("completed", "completed_with_warnings"):
        return PUBLIC_STATUS_COMPLETED
    if task_status == "failed":
        return PUBLIC_STATUS_FAILED
    if task_status == "cancelled":
        return PUBLIC_STATUS_CANCELLED
    return PUBLIC_STATUS_IN_PROGRESS


def project_completed_with_warnings(
    *,
    task_status: str,
    orchestration_status: str | None = None,
) -> bool:
    """带审核提醒完成信号（v1.2.6 presentation-only，不修改任何持久化状态）。

    仅当最新 orchestration（或 task 自身）确为 `completed_with_warnings` 时
    True —— 用于「已完成（包含审核提醒）」与「已完成」的展示区分。其它终态 /
    非终态一律 False。真实 DB 状态保持不变。
    """
    if orchestration_status == "completed_with_warnings":
        return True
    if orchestration_status is None and task_status == "completed_with_warnings":
        return True
    return False
