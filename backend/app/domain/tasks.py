"""Core domain enums for research tasks, shared by API, DB and future LangGraph state."""

from enum import StrEnum


class TaskStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TaskStage(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    COLLECTING = "collecting"
    PARSING = "parsing"
    EVIDENCE_EXTRACTION = "evidence_extraction"
    ANALYZING = "analyzing"
    SYNTHESIZING = "synthesizing"
    WRITING = "writing"
    CHECKING = "checking"
    AUDITING = "auditing"
    EXPORTING = "exporting"


class ResearchModule(StrEnum):
    COMPANY_PROFILE = "company_profile"
    BUSINESS = "business"
    FINANCIAL = "financial"
    EVENTS = "events"
    MACRO = "macro"
    RISK = "risk"


class WorkflowRunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class WorkflowEventType(StrEnum):
    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    NODE_COMPLETED = "node_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_WAITING_HUMAN = "run_waiting_human"
    RUN_RESUMED = "run_resumed"
    RUN_CANCELLED = "run_cancelled"


class HumanActionType(StrEnum):
    APPROVE_PLAN = "approve_plan"


ACTIVE_WORKFLOW_RUN_STATUSES = frozenset(
    {
        WorkflowRunStatus.PENDING,
        WorkflowRunStatus.RUNNING,
        WorkflowRunStatus.WAITING_HUMAN,
    }
)

TERMINAL_WORKFLOW_RUN_STATUSES = frozenset(
    {
        WorkflowRunStatus.COMPLETED,
        WorkflowRunStatus.FAILED,
        WorkflowRunStatus.CANCELLED,
    }
)

# reconcile 只处理不可能仍在本进程执行的 run
ORPHANED_WORKFLOW_RUN_STATUSES = frozenset(
    {
        WorkflowRunStatus.PENDING,
        WorkflowRunStatus.RUNNING,
    }
)
