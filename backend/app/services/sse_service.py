"""Pure SSE formatting helpers."""

import json

from app.core.errors import InvalidLastEventId
from app.schemas.workflow import WorkflowEventResponse


def format_sse_event(event: WorkflowEventResponse) -> str:
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {data}\n\n"


def parse_last_event_id(value: str | None) -> int:
    """解析 `Last-Event-ID` header 为 int cursor（run 级与 task 级 SSE 共用）。"""
    if value is None:
        return 0
    try:
        parsed = int(value)
    except ValueError:
        raise InvalidLastEventId() from None
    if parsed < 0:
        raise InvalidLastEventId()
    return parsed
