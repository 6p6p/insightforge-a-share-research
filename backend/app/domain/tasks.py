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
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
