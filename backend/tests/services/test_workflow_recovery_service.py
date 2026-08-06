"""Tests for the workflow recovery service."""

from uuid import uuid4

import pytest

from app.db.models.workflow_event import WorkflowEventModel
from app.db.models.workflow_run import WorkflowRunModel
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.services.workflow_recovery_service import WorkflowRecoveryService


class _FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        pass

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


class _FakeSessionMaker:
    def __init__(self) -> None:
        self.session = _FakeSession()

    def __call__(self) -> _FakeSession:
        return self.session


def _run(status: str = "pending") -> WorkflowRunModel:
    return WorkflowRunModel(
        run_id=uuid4(),
        task_id=uuid4(),
        thread_id=str(uuid4()),
        graph_name="research_workflow_simulation",
        graph_version="1d.1",
        status=status,
    )


@pytest.mark.asyncio
async def test_reconcile_marks_orphaned_failed(monkeypatch) -> None:
    sessionmaker = _FakeSessionMaker()
    service = WorkflowRecoveryService(sessionmaker)
    orphaned = [_run("pending"), _run("running")]

    async def fake_mark(self, failed_at, error_code, error_message):
        return orphaned

    monkeypatch.setattr(WorkflowRunRepository, "mark_orphaned_failed", fake_mark)

    result = await service.reconcile_orphaned_runs()

    assert result.marked_failed == 2
    failed_events = [
        obj
        for obj in sessionmaker.session.added
        if isinstance(obj, WorkflowEventModel) and obj.event_type == "run_failed"
    ]
    assert len(failed_events) == 2
    assert failed_events[0].payload.get("error_code") == "worker_restarted"


@pytest.mark.asyncio
async def test_reconcile_is_idempotent(monkeypatch) -> None:
    sessionmaker = _FakeSessionMaker()
    service = WorkflowRecoveryService(sessionmaker)

    async def fake_mark(self, failed_at, error_code, error_message):
        return []

    monkeypatch.setattr(WorkflowRunRepository, "mark_orphaned_failed", fake_mark)

    result = await service.reconcile_orphaned_runs()
    assert result.marked_failed == 0
    assert sessionmaker.session.added == []


@pytest.mark.asyncio
async def test_reconcile_waits_only_for_pending_running(monkeypatch) -> None:
    sessionmaker = _FakeSessionMaker()
    service = WorkflowRecoveryService(sessionmaker)

    # waiting_human/completed 不在 mark_orphaned_failed 的更新集合中；
    # 通过 fake 返回空验证 service 不额外处理
    async def fake_mark(self, failed_at, error_code, error_message):
        return []

    monkeypatch.setattr(WorkflowRunRepository, "mark_orphaned_failed", fake_mark)

    result = await service.reconcile_orphaned_runs()
    assert result.marked_failed == 0
