"""Tests for SSE formatting helpers."""

import json

from app.domain.tasks import WorkflowEventType
from app.schemas.workflow import WorkflowEventResponse
from app.services.sse_service import format_sse_event


def _event(**overrides: object) -> WorkflowEventResponse:
    defaults: dict = {
        "event_id": 3,
        "run_id": "00000000-0000-0000-0000-000000000001",
        "event_type": WorkflowEventType.NODE_COMPLETED,
        "node_name": "load_task_context",
        "stage": "created",
        "progress": 20,
        "message": "节点完成",
        "payload": {"completed_nodes": ["load_task_context"]},
        "created_at": "2026-01-01T00:00:00Z",
    }
    defaults.update(overrides)
    return WorkflowEventResponse.model_validate(defaults)


def test_format_sse_event_structure() -> None:
    text = format_sse_event(_event())
    lines = text.split("\n")
    assert lines[0] == "id: 3"
    assert lines[1] == "event: node_completed"
    assert lines[2].startswith("data: ")
    assert text.endswith("\n\n")


def test_format_chinese_json_is_valid_utf8() -> None:
    text = format_sse_event(_event(message="节点完成"))
    data = text.split("\n")[2][len("data: ") :]
    parsed = json.loads(data)
    assert parsed["message"] == "节点完成"


def test_format_compact_json() -> None:
    text = format_sse_event(_event())
    data = text.split("\n")[2][len("data: ") :]
    assert ": " not in data
    assert ", " not in data
