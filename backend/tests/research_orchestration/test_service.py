"""Research orchestration service unit tests（spec F/P/Q，0 DB）。

- `create_or_get_orchestration`：fingerprint replay（同输入 → 同 id + replayed=True）；
  新 fingerprint + task 已有 active → `ResearchOrchestrationActiveConflict`（409）；
  全新 → replayed=False；
- `cancel_orchestration`：minimal + 幂等（spec Q）——cancelled 原样返回、
  completed/failed → `AlreadyFinished`、active 时先取消 active child 再取消
  orchestration（不直接 SQL 删除）；
- `verify_orchestration_integrity`：重放 stored plan 的 planner fingerprint →
  不匹配 → `ResearchOrchestrationIntegrityError`。
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
    OrchestrationStatus,
    compute_orchestration_input_fingerprint,
)
from app.research_orchestration.errors import (
    ResearchOrchestrationActiveConflict,
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationIntegrityError,
)
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from app.research_orchestration.service import ResearchOrchestrationService
from app.research_planning.repository import ResearchPlanRepository
from tests.research_orchestration.fakes import (
    FakePlanService,
    FakeSessionMaker,
    make_orchestration,
)

pytestmark = pytest.mark.asyncio

_OID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
_PLAN_ID = UUID("00000000-0000-0000-0000-000000000003")
_FINGERPRINT = "b" * 64


def _service(sessionmaker) -> ResearchOrchestrationService:
    return ResearchOrchestrationService(sessionmaker, FakePlanService())


# ------------------------------------------------------------------ create / replay


async def test_create_or_get_replays_same_fingerprint(monkeypatch) -> None:
    """同输入（同 task + 同 plan input + 同 orchestrator）→ replay 同一 orchestration。"""
    sessionmaker = FakeSessionMaker()
    existing = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, research_plan_id=_PLAN_ID
    )

    async def fake_get_by_fp(self, fingerprint):
        return existing

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_input_fingerprint", fake_get_by_fp)
    result = await _service(sessionmaker).create_or_get_orchestration(_TASK_ID)
    assert result.replayed is True
    assert result.orchestration_id == _OID
    assert result.status == OrchestrationStatus.PENDING.value
    assert result.current_phase == "planning"


async def test_create_or_get_new_fingerprint_no_active(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    created_row = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, research_plan_id=_PLAN_ID
    )

    async def fake_get_by_fp(self, fingerprint):
        return None

    async def fake_get_active(self, task_id):
        return None

    async def fake_create_or_get(self, orchestration):
        return created_row, True

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_input_fingerprint", fake_get_by_fp)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_get_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "create_or_get", fake_create_or_get)
    result = await _service(sessionmaker).create_or_get_orchestration(_TASK_ID)
    assert result.replayed is False
    assert result.orchestration_id == _OID
    assert sessionmaker.session.committed is True


async def test_create_or_get_active_conflict(monkeypatch) -> None:
    """新 fingerprint（真 user retry）+ task 已有 active orchestration → 409。"""
    sessionmaker = FakeSessionMaker()

    async def fake_get_by_fp(self, fingerprint):
        return None

    async def fake_get_active(self, task_id):
        return make_orchestration(status=OrchestrationStatus.RUNNING.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_input_fingerprint", fake_get_by_fp)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_get_active)
    with pytest.raises(ResearchOrchestrationActiveConflict):
        await _service(sessionmaker).create_or_get_orchestration(_TASK_ID)


# ------------------------------------------------------------------ cancel


async def test_cancel_idempotent_when_already_cancelled(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()

    async def fake_get(self, orchestration_id):
        return make_orchestration(status=OrchestrationStatus.CANCELLED.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    result = await _service(sessionmaker).cancel_orchestration(_OID)
    assert result.status == OrchestrationStatus.CANCELLED.value


async def test_cancel_raises_already_finished_for_terminal(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()

    async def fake_get(self, orchestration_id):
        return make_orchestration(status=OrchestrationStatus.COMPLETED.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await _service(sessionmaker).cancel_orchestration(_OID)


async def test_cancel_cancels_active_child_then_orchestration(monkeypatch) -> None:
    """minimal cancel：先取消 active child，再 orchestration cancelled；不删行。"""
    sessionmaker = FakeSessionMaker()
    cancelled_runs: list = []

    async def fake_get(self, orchestration_id):
        return make_orchestration(status=OrchestrationStatus.RUNNING.value, current_phase="stage4")

    async def fake_list_children(self, orchestration_id):
        return [SimpleNamespace(workflow_run_id=UUID("00000000-0000-0000-0000-000000000009"))]

    async def fake_run_get(self, run_id):
        return SimpleNamespace(status="running")

    async def fake_mark_cancelled(self, run_id, cancelled_at):
        cancelled_runs.append(run_id)

    async def fake_orch_mark_cancelled(self, orchestration_id, completed_at):
        return make_orchestration(status=OrchestrationStatus.CANCELLED.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "list_children", fake_list_children)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)
    monkeypatch.setattr(WorkflowRunRepository, "mark_cancelled", fake_mark_cancelled)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_cancelled", fake_orch_mark_cancelled)
    result = await _service(sessionmaker).cancel_orchestration(_OID)
    assert result.status == OrchestrationStatus.CANCELLED.value
    assert len(cancelled_runs) == 1


# ------------------------------------------------------------------ verify integrity


def _plan_stub(planner_fp: str = "p" * 64, *, task_id: UUID = _TASK_ID):
    class _Plan:
        pass

    p = _Plan()
    p.task_id = task_id
    p.research_plan_id = _PLAN_ID
    p.planner_input_fingerprint = planner_fp
    return p


async def test_verify_integrity_ok(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    # 行里存储的 input_fingerprint 必须等于由 stored plan 的 planner fingerprint
    # 重算得到的 fp，否则校验会判定为"输入被篡改"。
    stored_fp = compute_orchestration_input_fingerprint(
        orchestration_schema_version=ORCHESTRATION_SCHEMA_VERSION,
        task_id=_TASK_ID,
        planner_input_fingerprint="p" * 64,
        orchestrator_name=ORCHESTRATOR_NAME,
        orchestrator_version=ORCHESTRATOR_VERSION,
    )
    row = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        current_phase="planning",
        input_fingerprint=stored_fp,
    )

    async def fake_get(self, orchestration_id):
        return row

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    result = await _service(sessionmaker).verify_orchestration_integrity(_OID)
    assert result is row


async def test_verify_integrity_fingerprint_mismatch(monkeypatch) -> None:
    """stored plan 的 planner input fingerprint 与 orchestration 行不一致 →
    integrity error（输入被篡改）。"""
    sessionmaker = FakeSessionMaker()
    row = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        input_fingerprint="c" * 64,
    )

    async def fake_get(self, orchestration_id):
        return row

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub(planner_fp="q" * 64)  # 与行里存储的 fp 不匹配

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    with pytest.raises(ResearchOrchestrationIntegrityError):
        await _service(sessionmaker).verify_orchestration_integrity(_OID)


async def test_verify_integrity_missing_plan(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    row = make_orchestration(orchestration_id=_OID, task_id=_TASK_ID, research_plan_id=_PLAN_ID)

    async def fake_get(self, orchestration_id):
        return row

    async def fake_plan_get(self, research_plan_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    with pytest.raises(ResearchOrchestrationIntegrityError):
        await _service(sessionmaker).verify_orchestration_integrity(_OID)
