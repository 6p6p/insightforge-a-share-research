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
