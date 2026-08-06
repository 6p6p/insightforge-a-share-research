"""Tests for the workflow run repository update semantics."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.db.models.workflow_run import WorkflowRunModel
from app.repositories.workflow_run_repository import WorkflowRunRepository

pytestmark = pytest.mark.asyncio


class _EmptySession:
    def add(self, obj) -> None:
        pass

    async def flush(self) -> None:
        pass


def _run(**overrides: object) -> WorkflowRunModel:
    defaults: dict = {
        "run_id": uuid4(),
        "task_id": uuid4(),
        "thread_id": str(uuid4()),
        "graph_name": "research_workflow_simulation",
        "graph_version": "1b.1",
        "status": "pending",
    }
    defaults.update(overrides)
    return WorkflowRunModel(**defaults)


async def test_mark_running_updates_fields(monkeypatch) -> None:
    repo = WorkflowRunRepository(_EmptySession())
    run = _run()

    async def fake_get(run_id):
        return run

    monkeypatch.setattr(repo, "get_by_id", fake_get)
    started = datetime.now(UTC)
    updated = await repo.mark_running(run.run_id, started)
    assert updated.status == "running"
    assert updated.started_at == started


async def test_mark_completed_updates_fields(monkeypatch) -> None:
    repo = WorkflowRunRepository(_EmptySession())
    run = _run()

    async def fake_get(run_id):
        return run

    monkeypatch.setattr(repo, "get_by_id", fake_get)
    completed = datetime.now(UTC)
    updated = await repo.mark_completed(run.run_id, completed)
    assert updated.status == "completed"
    assert updated.completed_at == completed


async def test_mark_failed_updates_fields(monkeypatch) -> None:
    repo = WorkflowRunRepository(_EmptySession())
    run = _run()

    async def fake_get(run_id):
        return run

    monkeypatch.setattr(repo, "get_by_id", fake_get)
    failed = datetime.now(UTC)
    updated = await repo.mark_failed(
        run.run_id,
        failed,
        "graph_execution_failed",
        "ValueError",
    )
    assert updated.status == "failed"
    assert updated.failed_at == failed
    assert updated.error_code == "graph_execution_failed"
    assert updated.error_message == "ValueError"


async def test_mark_missing_returns_none(monkeypatch) -> None:
    repo = WorkflowRunRepository(_EmptySession())

    async def fake_get(run_id):
        return None

    monkeypatch.setattr(repo, "get_by_id", fake_get)
    assert await repo.mark_completed(uuid4(), datetime.now(UTC)) is None
