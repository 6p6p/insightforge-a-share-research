"""Pure SSE formatting helpers."""

import json

from app.schemas.workflow import WorkflowEventResponse


def format_sse_event(event: WorkflowEventResponse) -> str:
    data = json.dumps(event.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":"))
    return f"id: {event.event_id}\nevent: {event.event_type.value}\ndata: {data}\n\n"
