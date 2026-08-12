"""ResearchOrchestrationChildService unit tests（spec C/D/K，7A.2B.2 spec E/F/J/K，0 DB）。

- exact child `(orchestration_id, stage4/stage5, attempt 1)` 已存在 → 复用
  （created=False），**不重复 create**（绝不用 latest task + graph 猜归属）；
- 不存在 → `create_stage4_run / create_stage5_run(on_run_created=...)`：hook 收到
  (session, run_id) 并在同一事务 add child link（same-transaction 语义由集成 Case
  验证；这里验证 hook 被调用 + stage/attempt_no 锚定）；
- 并发 `ActiveWorkflowRunExists` → 重查 exact child → 返回 winner；
- child ownership 约束 IntegrityError → 重查无 winner → `ResearchOrchestrationChildConflict`
  （409，spec E）；有 winner → 复用；其它 IntegrityError → 原样抛；
- stage5 runner 未绑定 → `ensure_stage5_child` 抛 RuntimeError（programming error）。
"""

from types import SimpleNamespace
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from app.core.errors import ActiveWorkflowRunExists
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.errors import ResearchOrchestrationChildConflict
from app.research_orchestration.repository import ResearchOrchestrationChildRepository
from app.research_orchestration.service import ResearchOrchestrationChildService
from tests.research_orchestration.fakes import (
    FakeSessionMaker,
    FakeStage4Runner,
    FakeStage5Runner,
)

pytestmark = pytest.mark.asyncio

_OID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
_CHILD_RUN_ID = UUID("00000000-0000-0000-0000-000000000009")
_REQUEST = object()


def _child(workflow_run_id: UUID):
    return SimpleNamespace(orchestration_id=_OID, workflow_run_id=workflow_run_id)


def _integrity_error(constraint_name: str) -> IntegrityError:
    """构造带 PostgreSQL diag constraint_name 的 IntegrityError（0 DB）。

    SQLAlchemy `IntegrityError(statement, params, orig)` 的 `exc.orig` 就是第三个
    参数；psycopg 原生异常带 `.diag` → 传 `SimpleNamespace(diag=...)` 使
    `getattr(exc.orig, "diag", None)` 返回 diag。
    """
    diag = SimpleNamespace(constraint_name=constraint_name)
    orig = SimpleNamespace(diag=diag)
    return IntegrityError("stmt", {}, orig)


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
    assert runner.child_binds == []  # 未调 create_stage4_run / on_run_created


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
    # on_run_created hook 收到 (session, run_id)，同一事务 add child link。
    assert len(runner.child_binds) == 1
    assert runner.child_binds[0].workflow_run_id == runner.run_id
    assert runner.child_binds[0].orchestration_id == _OID
    assert runner.child_binds[0].attempt_no == 1


async def test_child_ownership_conflict_raises_409(monkeypatch) -> None:
    """child link 归属约束 IntegrityError + 无 exact child → 409（spec E）。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner(
        fail_with=_integrity_error("uq_research_orchestration_child_runs_scope_attempt")
    )

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    with pytest.raises(ResearchOrchestrationChildConflict):
        await ResearchOrchestrationChildService(sessionmaker, runner).ensure_stage4_child(
            _OID, _REQUEST
        )


async def test_child_ownership_conflict_with_winner_returns_winner(monkeypatch) -> None:
    """child link 归属冲突但 exact child 已存在（并发 winner）→ 复用，不抛 409。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner(
        fail_with=_integrity_error("uq_research_orchestration_child_runs_workflow_run_id")
    )
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


async def test_other_integrity_error_re_raises(monkeypatch) -> None:
    """非 child ownership 约束的 IntegrityError → 原样抛（runner 不猜语义）。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeStage4Runner(fail_with=_integrity_error("some_other_constraint"))

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    with pytest.raises(IntegrityError):
        await ResearchOrchestrationChildService(sessionmaker, runner).ensure_stage4_child(
            _OID, _REQUEST
        )


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


# ------------------------------------------------------------------ stage5


async def test_stage5_existing_child_is_reused_not_created(monkeypatch) -> None:
    """exact child `(orchestration_id, stage5, 1)` 已存在 → 复用，不重复 create。"""
    sessionmaker = FakeSessionMaker()
    stage5_runner = FakeStage5Runner()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return _child(_CHILD_RUN_ID)

    async def fake_run_get(self, run_id):
        return SimpleNamespace(run_id=_CHILD_RUN_ID)

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    service = ResearchOrchestrationChildService(
        sessionmaker, FakeStage4Runner(), stage5_runner=stage5_runner
    )
    result = await service.ensure_stage5_child(_OID, _REQUEST)
    assert result.run_id == _CHILD_RUN_ID
    assert result.created is False
    assert stage5_runner.child_binds == []  # 未调 create_stage5_run / on_run_created


async def test_stage5_missing_child_creates_with_bind(monkeypatch) -> None:
    """不存在 → `create_stage5_run(on_run_created=...)` 绑定 stage5 + attempt 1。"""
    sessionmaker = FakeSessionMaker()
    stage5_runner = FakeStage5Runner()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    service = ResearchOrchestrationChildService(
        sessionmaker, FakeStage4Runner(), stage5_runner=stage5_runner
    )
    result = await service.ensure_stage5_child(_OID, _REQUEST)
    assert result.created is True
    assert result.run_id == stage5_runner.run_id
    assert len(stage5_runner.child_binds) == 1
    bind = stage5_runner.child_binds[0]
    assert bind.workflow_run_id == stage5_runner.run_id
    assert bind.orchestration_id == _OID
    assert bind.stage == "stage5"  # ChildStage.STAGE5.value——exact 锚定，非 latest
    assert bind.attempt_no == 1


async def test_stage5_ownership_conflict_raises_409(monkeypatch) -> None:
    """stage5 child link 归属约束 IntegrityError + 无 exact child → 409。"""
    sessionmaker = FakeSessionMaker()
    stage5_runner = FakeStage5Runner(
        fail_with=_integrity_error("uq_research_orchestration_child_runs_workflow_run_id")
    )

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    service = ResearchOrchestrationChildService(
        sessionmaker, FakeStage4Runner(), stage5_runner=stage5_runner
    )
    with pytest.raises(ResearchOrchestrationChildConflict):
        await service.ensure_stage5_child(_OID, _REQUEST)


async def test_stage5_concurrent_create_requeries_exact_child_winner(monkeypatch) -> None:
    """并发：stage5 create 时 active index 冲突 → 重查 exact child → 返回 winner。"""
    sessionmaker = FakeSessionMaker()
    stage5_runner = FakeStage5Runner(fail_with=ActiveWorkflowRunExists())
    get_child_results = [None, _child(_CHILD_RUN_ID)]

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return get_child_results.pop(0)

    async def fake_run_get(self, run_id):
        return SimpleNamespace(run_id=_CHILD_RUN_ID)

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    service = ResearchOrchestrationChildService(
        sessionmaker, FakeStage4Runner(), stage5_runner=stage5_runner
    )
    result = await service.ensure_stage5_child(_OID, _REQUEST)
    assert result.run_id == _CHILD_RUN_ID
    assert result.created is False


async def test_stage5_unbound_runner_raises(monkeypatch) -> None:
    """stage5 runner 未绑定 → RuntimeError（programming error，不猜归属）。"""
    sessionmaker = FakeSessionMaker()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    service = ResearchOrchestrationChildService(sessionmaker, FakeStage4Runner())
    with pytest.raises(RuntimeError, match="stage5 runner not bound"):
        await service.ensure_stage5_child(_OID, _REQUEST)
