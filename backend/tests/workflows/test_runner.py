"""Tests for the workflow runner control flow using fakes."""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.core.errors import (
    ActiveWorkflowRunExists,
    TaskNotFound,
    WorkflowRunAlreadyFinished,
    WorkflowRunAlreadyStarted,
)
from app.db.models.human_action import HumanActionModel
from app.db.models.research_task import ResearchTaskModel
from app.db.models.workflow_event import WorkflowEventModel
from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import HumanActionType
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.workflows.graph import build_research_workflow
from app.workflows.runner import WorkflowRunner

pytestmark = pytest.mark.asyncio


class FakeSession:
    def __init__(self) -> None:
        self.closed = False
        self.commits = 0
        self.added: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        self.closed = True

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        pass

    def add(self, obj) -> None:
        self.added.append(obj)

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
        self._saver = InMemorySaver()
        self.checkpointers: list = []

    async def get_checkpointer(self):
        self.checkpointers.append(self._saver)
        return self._saver


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


def _event_types(sessionmaker: FakeSessionMaker) -> list[str]:
    types: list[str] = []
    for session in sessionmaker.sessions:
        for obj in session.added:
            if isinstance(obj, WorkflowEventModel):
                types.append(obj.event_type)
    return types


async def test_create_run_creates_run_created_event(monkeypatch) -> None:
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
    assert _event_types(sessionmaker) == ["run_created"]
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


async def test_execute_success_event_sequence(monkeypatch) -> None:
    task = _task()
    run = _run(task_id=task.task_id, status="pending")
    sessionmaker = FakeSessionMaker()
    runner = WorkflowRunner(sessionmaker, FakeCheckpointManager())

    async def fake_claim(self, run_id, started_at):
        run.status = "running"
        run.started_at = started_at
        return run

    async def fake_get_task(self, task_id):
        return task

    async def fake_mark_completed(self, run_id, completed_at):
        run.status = "completed"
        run.completed_at = completed_at
        return run

    async def fake_get_run(self, run_id):
        return run

    monkeypatch.setattr(WorkflowRunRepository, "claim_pending", fake_claim)
    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "mark_completed", fake_mark_completed)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)

    result = await runner.execute_simulation(run.run_id)

    assert result["simulation_complete"] is True
    assert run.status == "completed"
    assert _event_types(sessionmaker) == [
        "run_started",
        "node_completed",
        "node_completed",
        "node_completed",
        "node_completed",
        "run_completed",
    ]
    assert task.status == "pending"
    assert task.current_stage == "created"
    assert task.progress == 0
    # 事件 payload 不包含完整 research_plan
    for session in sessionmaker.sessions:
        for obj in session.added:
            if isinstance(obj, WorkflowEventModel):
                assert "research_plan" not in obj.payload
    # run_completed 使用最终 current_stage（planning），而非伪造 exporting
    completed_events = [
        obj
        for session in sessionmaker.sessions
        for obj in session.added
        if isinstance(obj, WorkflowEventModel) and obj.event_type == "run_completed"
    ]
    assert completed_events
    assert completed_events[0].stage == "planning"


async def test_execute_failure_marks_failed(monkeypatch) -> None:
    task = _task()
    run = _run(task_id=task.task_id, status="pending")
    sessionmaker = FakeSessionMaker()

    class _FailingGraph:
        async def astream(self, state, config, **kwargs):
            raise ValueError("simulated graph failure")
            yield

        async def aget_state(self, config):
            return None

    runner = WorkflowRunner(sessionmaker, FakeCheckpointManager())

    async def fake_claim(self, run_id, started_at):
        run.status = "running"
        return run

    async def fake_get_task(self, task_id):
        return task

    async def fake_mark_failed(self, run_id, failed_at, error_code, error_message):
        run.status = "failed"
        run.failed_at = failed_at
        run.error_code = error_code
        run.error_message = error_message
        return run

    monkeypatch.setattr(WorkflowRunRepository, "claim_pending", fake_claim)
    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "mark_failed", fake_mark_failed)
    monkeypatch.setattr(
        "app.workflows.runner.build_research_workflow",
        lambda checkpointer: _FailingGraph(),
    )

    with pytest.raises(ValueError):
        await runner.execute_simulation(run.run_id)

    assert run.status == "failed"
    assert run.error_code == "workflow_execution_failed"
    assert run.error_message == "ValueError"
    assert _event_types(sessionmaker)[-1] == "run_failed"


async def test_execute_running_run_raises(monkeypatch) -> None:
    run = _run(status="running")
    runner = WorkflowRunner(FakeSessionMaker(), FakeCheckpointManager())

    async def fake_claim(self, run_id, started_at):
        return None

    async def fake_get_run(self, run_id):
        return run

    monkeypatch.setattr(WorkflowRunRepository, "claim_pending", fake_claim)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)

    with pytest.raises(WorkflowRunAlreadyStarted):
        await runner.execute_simulation(run.run_id)


async def test_execute_finished_run_raises(monkeypatch) -> None:
    run = _run(status="completed")
    runner = WorkflowRunner(FakeSessionMaker(), FakeCheckpointManager())

    async def fake_claim(self, run_id, started_at):
        return None

    async def fake_get_run(self, run_id):
        return run

    monkeypatch.setattr(WorkflowRunRepository, "claim_pending", fake_claim)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)

    with pytest.raises(WorkflowRunAlreadyFinished):
        await runner.execute_simulation(run.run_id)


async def test_execute_interrupt_marks_waiting_human(monkeypatch) -> None:
    task = _task(require_plan_approval=True)
    run = _run(task_id=task.task_id, status="pending")
    sessionmaker = FakeSessionMaker()
    runner = WorkflowRunner(sessionmaker, FakeCheckpointManager())

    async def fake_claim(self, run_id, started_at):
        run.status = "running"
        return run

    async def fake_get_task(self, task_id):
        return task

    async def fake_mark_waiting(self, run_id, pending_action):
        run.status = "waiting_human"
        run.pending_action = pending_action
        return run

    async def fake_get_run(self, run_id):
        return run

    monkeypatch.setattr(WorkflowRunRepository, "claim_pending", fake_claim)
    monkeypatch.setattr(ResearchTaskRepository, "get_by_id", fake_get_task)
    monkeypatch.setattr(WorkflowRunRepository, "mark_waiting_human", fake_mark_waiting)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)

    await runner.execute_simulation(run.run_id)

    assert run.status == "waiting_human"
    assert run.pending_action == "plan_approval"
    assert "run_waiting_human" in _event_types(sessionmaker)
    assert "run_completed" not in _event_types(sessionmaker)


async def test_resume_completes_after_approval(monkeypatch) -> None:
    task = _task(require_plan_approval=True)
    run = _run(task_id=task.task_id, status="waiting_human", pending_action="plan_approval")
    sessionmaker = FakeSessionMaker()
    checkpoint = FakeCheckpointManager()
    runner = WorkflowRunner(sessionmaker, checkpoint)

    # 先在共享 saver 上产生 interrupt checkpoint
    graph = build_research_workflow(await checkpoint.get_checkpointer())
    config = {"configurable": {"thread_id": run.thread_id}}
    initial = {
        "task_id": str(task.task_id),
        "run_id": str(run.run_id),
        "company_query": task.company_query,
        "modules": task.modules,
        "questions": task.questions,
        "current_stage": task.current_stage,
        "progress": task.progress,
        "require_plan_approval": True,
    }
    async for _ in graph.astream(initial, config, stream_mode="updates"):
        pass
    snapshot = await graph.aget_state(config)
    assert any(t.interrupts for t in snapshot.tasks)

    async def fake_claim_waiting(self, run_id, started_at):
        run.status = "running"
        run.pending_action = None
        return run

    async def fake_mark_completed(self, run_id, completed_at):
        run.status = "completed"
        run.completed_at = completed_at
        return run

    async def fake_get_run(self, run_id):
        return run

    monkeypatch.setattr(WorkflowRunRepository, "claim_waiting_human", fake_claim_waiting)
    monkeypatch.setattr(WorkflowRunRepository, "mark_completed", fake_mark_completed)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_get_run)

    preparation = await runner.prepare_resume(run.run_id, HumanActionType.APPROVE_PLAN)
    assert preparation.thread_id == run.thread_id
    result = await runner.continue_resume(preparation)

    assert result["simulation_complete"] is True
    assert run.status == "completed"
    types = _event_types(sessionmaker)
    assert "run_resumed" in types
    assert "run_completed" in types
    actions = [
        obj
        for session in sessionmaker.sessions
        for obj in session.added
        if isinstance(obj, HumanActionModel)
    ]
    assert len(actions) == 1
    assert actions[0].action_type == "approve_plan"
