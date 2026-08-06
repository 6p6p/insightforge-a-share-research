"""Domain errors mapped to stable API error responses."""


class DomainError(Exception):
    code: str = "domain_error"
    http_status: int = 500
    message: str = "domain error"


class TaskNotFound(DomainError):
    code = "task_not_found"
    http_status = 404
    message = "研究任务不存在"


class IdempotencyConflict(DomainError):
    code = "idempotency_conflict"
    http_status = 409
    message = "幂等键已用于不同的请求内容"


class InvalidIdempotencyKey(DomainError):
    code = "invalid_idempotency_key"
    http_status = 400
    message = "Idempotency-Key 格式非法"


class WorkflowRunNotFound(DomainError):
    code = "workflow_run_not_found"
    http_status = 404
    message = "工作流运行不存在"


class ActiveWorkflowRunExists(DomainError):
    code = "active_workflow_run_exists"
    http_status = 409
    message = "该任务已存在进行中的工作流运行"


class WorkflowRunAlreadyFinished(DomainError):
    code = "workflow_run_already_finished"
    http_status = 409
    message = "工作流运行已结束，不能重复执行"


class WorkflowRunAlreadyStarted(DomainError):
    code = "workflow_run_already_started"
    http_status = 409
    message = "工作流运行已经开始执行"


class InvalidLastEventId(DomainError):
    code = "invalid_last_event_id"
    http_status = 400
    message = "Last-Event-ID 必须是大于等于 0 的整数"
