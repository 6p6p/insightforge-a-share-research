"""Tests for the workflow runner control flow using fakes."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.core.errors import (
    ActiveWorkflowRunExists,
    TaskNotFound,
    WorkflowRunAlreadyFinished,
)
from app.db.models.research_task import ResearchTaskModel
from app.db.models.workflow_run import WorkflowRunModel
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.workflows.runner import WorkflowRunner

pytestmark = pytest.mark.asyncio


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        self.closed = True

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    def add(self, obj) -> None:
        pass

    async def flush(self) -> None:
        pass


class FakeSessionMaker:
    def __init__(self) -> None:
        self.sessions: list[FakeSession] = []

    def __call__(self) -> FakeSession:
        session = FakeSession()
        self.sessions.append(session)
        return session


class FakeCheckpointManager:
    def __init__(self) -> None:
        self.checkpointers: list = []

    async def get_checkpointer(self):
        saver = InMemorySaver()
        self.checkpointers.append(saver)
        return saver


def _task(**overrides: object) -> ResearchTaskModel:
    defaults: dict = {
        "task_id": uuid4(),
        "company_query": "600519",
        "research_start_date": date(2023, 1, 1),
        "research_end_date": date(2025, 12, 31),
        "modules": ["company_profile"],
        "questions": [],
        "status": "pending",
        "current_stage": "created",
        "progress": 0,
    }
    defaults.update(overrides)
    return ResearchTaskModel(**defaults)


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


async def test_create_run_fields_and_transaction(monkeypatch) -> None:
    task = _task()
    sessionmaker = FakeSessionMaker()
    runner = WorkflowRunner(sessionmaker, FakeCheckpointManager())

    async def fake_get_task(self, task_id):
        return task

    async def fake_get_active(self, task_id):
        return None

    async def fake_create(self, run):
        run.created_at = datetime.now(UTC)
        run.updated_at = datetime.now(UTC)
        return run

    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "get_active_for_task", fake_get_active)
    monkeypatch.setattr(WorkflowRunRepository, "create", fake_create)

    result = await runner.create_simulation_run(task.task_id)

    assert result.status.value == "pending"
    assert result.thread_id == str(result.run_id)
    assert result.graph_name == "research_workflow_simulation"
    assert result.graph_version == "1b.1"
    assert sessionmaker.sessions[0].closed is True
    assert sessionmaker.sessions[0].commits == 1


async def test_create_run_missing_task_raises(monkeypatch) -> None:
    runner = WorkflowRunner(FakeSessionMaker(), FakeCheckpointManager())

    async def fake_get_task(self, task_id):
        return None

    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)

    with pytest.raises(TaskNotFound):
        await runner.create_simulation_run(uuid4())


async def test_create_run_active_exists_raises(monkeypatch) -> None:
    task = _task()
    active = _run(task_id=task.task_id, status="running")
    runner = WorkflowRunner(FakeSessionMaker(), FakeCheckpointManager())

    async def fake_get_task(self, task_id):
        return task

    async def fake_get_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "get_active_for_task", fake_get_active)

    with pytest.raises(ActiveWorkflowRunExists):
        await runner.create_simulation_run(task.task_id)


async def test_execute_success_marks_completed(monkeypatch) -> None:
    task = _task()
    run = _run(task_id=task.task_id, status="pending")
    sessionmaker = FakeSessionMaker()
    runner = WorkflowRunner(sessionmaker, FakeCheckpointManager())

    async def fake_get_run(self, run_id):
        return run

    async def fake_get_task(self, task_id):
        return task

    async def fake_mark_completed(self, run_id, completed_at):
        run.status = "completed"
        run.completed_at = completed_at
        return run

    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)
    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "mark_completed", fake_mark_completed)

    result = await runner.execute_simulation(run.run_id)

    assert result["simulation_complete"] is True
    assert run.status == "completed"
    # 短事务读 + 短事务标记 = 2 个 session；graph 执行期间不持有 session
    assert len(sessionmaker.sessions) == 2
    assert all(session.closed for session in sessionmaker.sessions)
    assert task.status == "pending"
    assert task.current_stage == "created"
    assert task.progress == 0


async def test_execute_failure_marks_failed_and_raises(monkeypatch) -> None:
    task = _task()
    run = _run(task_id=task.task_id, status="pending")
    sessionmaker = FakeSessionMaker()

    class _FailingGraph:
        async def ainvoke(self, state, config):
            raise ValueError("simulated graph failure")

    runner = WorkflowRunner(sessionmaker, FakeCheckpointManager())

    async def fake_get_run(self, run_id):
        return run

    async def fake_get_task(self, task_id):
        return task

    async def fake_mark_failed(self, run_id, failed_at, error_code, error_message):
        run.status = "failed"
        run.failed_at = failed_at
        run.error_code = error_code
        run.error_message = error_message
        return run

    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)
    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(
        "app.workflows.runner.build_research_workflow",
        lambda checkpointer: _FailingGraph(),
    )

    with pytest.raises(ValueError):
        await runner.execute_simulation(run.run_id)

    assert run.status == "failed"
    assert run.error_code == "graph_execution_failed"
    assert run.error_message == "ValueError"


async def test_execute_finished_run_raises(monkeypatch) -> None:
    run = _run(status="completed")
    runner = WorkflowRunner(FakeSessionMaker(), FakeCheckpointManager())

    async def fake_get_run(self, run_id):
        return run

    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)

    with pytest.raises(WorkflowRunAlreadyFinished):
        await runner.execute_simulation(run.run_id)
