"""Tests for workflow event enums and schemas."""

import json

import pytest
from pydantic import ValidationError

from app.domain.tasks import WorkflowEventType
from app.schemas.workflow import WorkflowEventResponse


def test_event_type_values_are_stable() -> None:
    assert [event_type.value for event_type in WorkflowEventType] == [
        "run_created",
        "run_started",
        "node_completed",
        "run_completed",
        "run_failed",
        "run_waiting_human",
        "run_resumed",
        "run_cancelled",
    ]


def _event(**overrides: object) -> WorkflowEventResponse:
    defaults: dict = {
        "event_id": 1,
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


def test_event_response_valid() -> None:
    event = _event()
    assert event.event_type.value == "node_completed"
    assert event.progress == 20


def test_event_progress_out_of_range_rejected() -> None:
    with pytest.raises(ValidationError):
        _event(progress=101)
    with pytest.raises(ValidationError):
        _event(progress=-1)


def test_event_payload_is_json_serializable() -> None:
    event = _event(payload={"completed_nodes": ["a", "b"], "simulation_complete": False})
    json.dumps(event.model_dump(mode="json"))
