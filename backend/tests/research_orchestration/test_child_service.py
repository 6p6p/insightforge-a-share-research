"""ResearchOrchestrationChildService unit tests（spec C/D/K，0 DB）。

- exact child `(orchestration_id, stage4, attempt 1)` 已存在 → 复用（created=False），
  **不重复 create**；
- 不存在 → `create_stage4_run(child_bind=...)`：child_bind 收到 run_id 并在同一
  事务提交（same-transaction 语义由集成 Case 1 验证；这里验证 child_bind 被调用）；
- 并发 `ActiveWorkflowRunExists` → 重查 exact child → 返回 winner。
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.errors import ActiveWorkflowRunExists
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.repository import ResearchOrchestrationChildRepository
from app.research_orchestration.service import ResearchOrchestrationChildService
from tests.research_orchestration.fakes import FakeSessionMaker, FakeStage4Runner

pytestmark = pytest.mark.asyncio

_OID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
_CHILD_RUN_ID = UUID("00000000-0000-0000-0000-000000000009")
_REQUEST = object()


def _child(workflow_run_id: UUID):
    return SimpleNamespace(orchestration_id=_OID, workflow_run_id=workflow_run_id)


async def test_existing_child_is_reused_not_created(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return _child(_CHILD_RUN_ID)

    async def fake_run_get(self, run_id):
        return SimpleNamespace(run_id=_CHILD_RUN_ID)

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    result = await ResearchOrchestrationChildService(sessionmaker, runner).ensure_stage4_child(
        _OID, _REQUEST
    )
    assert result.run_id == _CHILD_RUN_ID
    assert result.created is False
    assert runner.child_binds == []  # 未调 create_stage4_run / child_bind


async def test_missing_child_creates_with_bind(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    result = await ResearchOrchestrationChildService(sessionmaker, runner).ensure_stage4_child(
        _OID, _REQUEST
    )
    assert result.created is True
    assert result.run_id == runner.run_id
    # child_bind 收到 run_id 并构造了 ResearchOrchestrationChildModel。
    assert len(runner.child_binds) == 1
    assert runner.child_binds[0].workflow_run_id == runner.run_id
    assert runner.child_binds[0].orchestration_id == _OID
    assert runner.child_binds[0].attempt_no == 1


async def test_concurrent_create_requeries_exact_child_winner(monkeypatch) -> None:
    """并发：create 时 active index 冲突 → 重查 exact child → 返回 winner。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner(fail_with=ActiveWorkflowRunExists())
    get_child_results = [None, _child(_CHILD_RUN_ID)]

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return get_child_results.pop(0)

    async def fake_run_get(self, run_id):
        return SimpleNamespace(run_id=_CHILD_RUN_ID)

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    result = await ResearchOrchestrationChildService(sessionmaker, runner).ensure_stage4_child(
        _OID, _REQUEST
    )
    assert result.run_id == _CHILD_RUN_ID
    assert result.created is False


async def test_concurrent_create_no_winner_re_raises(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner(fail_with=ActiveWorkflowRunExists())

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    with pytest.raises(ActiveWorkflowRunExists):
        await ResearchOrchestrationChildService(sessionmaker, runner).ensure_stage4_child(
            _OID, _REQUEST
        )
