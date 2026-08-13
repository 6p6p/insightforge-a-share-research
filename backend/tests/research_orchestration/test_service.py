"""Research orchestration service unit tests（spec F/P/Q + 7A.2B.2 spec B/C，0 DB）。

- `create_or_get_orchestration`：同 plan + attempt=1 replay（同输入 → 同 id +
  replayed=True）；新 fingerprint + task 已有 active → `ResearchOrchestrationActiveConflict`
  （409）；全新 → replayed=False；
- `retry_orchestration`（7A.2B.2 spec C）：failed/cancelled → **NEW orchestration_id**、
  attempt+1、retry_of=old、fingerprint 相同、old 不变；completed/active → reject；
  并发 retry（latest.attempt>old.attempt 且 retry_of=old）→ 返回 winner；
- `cancel_orchestration`：minimal + 幂等（spec Q）——cancelled 原样返回、
  completed/failed → `AlreadyFinished`、active 时先取消 active child 再取消
  orchestration（不直接 SQL 删除）；
- `verify_orchestration_integrity`：重放 stored plan 的 planner fingerprint →
  不匹配 → `ResearchOrchestrationIntegrityError`；retry_of 必须同 task/plan（spec B）。
"""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
    RESUME_KIND_PREPARE,
    RESUME_KIND_SUPPLEMENTAL_RESEARCH,
    OrchestrationStatus,
    compute_orchestration_input_fingerprint,
)
from app.research_orchestration.errors import (
    ResearchOrchestrationActiveConflict,
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationIntegrityError,
    ResearchOrchestrationInvalidAction,
    ResearchOrchestrationNotFound,
    ResearchOrchestrationRetryRequired,
)
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from app.research_orchestration.service import ResearchOrchestrationService
from app.research_planning.repository import ResearchPlanRepository
from tests.research_orchestration.fakes import (
    FakeExecutionManager,
    FakePlanService,
    FakeSessionMaker,
    make_orchestration,
)

pytestmark = pytest.mark.asyncio

_OID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
_PLAN_ID = UUID("00000000-0000-0000-0000-000000000003")
_FINGERPRINT = "b" * 64
_RETRY_ID = UUID("00000000-0000-0000-0000-00000000000a")


def _stored_fp(planner_fp: str = "p" * 64) -> str:
    """与 `_plan_stub` 默认 planner fp 匹配的 orchestration input fingerprint。"""
    return compute_orchestration_input_fingerprint(
        orchestration_schema_version=ORCHESTRATION_SCHEMA_VERSION,
        task_id=_TASK_ID,
        planner_input_fingerprint=planner_fp,
        orchestrator_name=ORCHESTRATOR_NAME,
        orchestrator_version=ORCHESTRATOR_VERSION,
    )


def _service(sessionmaker) -> ResearchOrchestrationService:
    return ResearchOrchestrationService(sessionmaker, FakePlanService())


# ------------------------------------------------------------------ create / replay


async def test_create_or_get_replays_same_plan_attempt1(monkeypatch) -> None:
    """同输入（同 plan + attempt=1）→ replay 同一 orchestration（replayed=True）。"""
    sessionmaker = FakeSessionMaker()
    existing = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, research_plan_id=_PLAN_ID
    )

    async def fake_get_by_plan_attempt(self, research_plan_id, attempt_no):
        assert attempt_no == 1
        return existing

    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_plan_and_attempt", fake_get_by_plan_attempt
    )
    result = await _service(sessionmaker).create_or_get_orchestration(_TASK_ID)
    assert result.replayed is True
    assert result.orchestration_id == _OID
    assert result.status == OrchestrationStatus.PENDING.value
    assert result.current_phase == "planning"


async def test_create_or_get_new_plan_no_active(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    created_row = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, research_plan_id=_PLAN_ID
    )

    async def fake_get_by_plan_attempt(self, research_plan_id, attempt_no):
        return None

    async def fake_get_active(self, task_id):
        return None

    async def fake_create_or_get(self, orchestration):
        return created_row, True

    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_plan_and_attempt", fake_get_by_plan_attempt
    )
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_get_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "create_or_get", fake_create_or_get)
    result = await _service(sessionmaker).create_or_get_orchestration(_TASK_ID)
    assert result.replayed is False
    assert result.orchestration_id == _OID
    assert sessionmaker.session.committed is True


async def test_create_or_get_active_conflict(monkeypatch) -> None:
    """新 plan + task 已有 active orchestration → 409。"""
    sessionmaker = FakeSessionMaker()

    async def fake_get_by_plan_attempt(self, research_plan_id, attempt_no):
        return None

    async def fake_get_active(self, task_id):
        return make_orchestration(status=OrchestrationStatus.RUNNING.value)

    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_plan_and_attempt", fake_get_by_plan_attempt
    )
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_get_active)
    with pytest.raises(ResearchOrchestrationActiveConflict):
        await _service(sessionmaker).create_or_get_orchestration(_TASK_ID)


# ------------------------------------------------------------------ retry


async def test_retry_creates_new_attempt(monkeypatch) -> None:
    """failed old → retry → NEW orchestration_id、attempt=2、retry_of=old、
    fingerprint/task/plan 相同、status=pending、old 行不被修改。"""
    sessionmaker = FakeSessionMaker()
    old = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        status=OrchestrationStatus.FAILED.value,
        input_fingerprint=_stored_fp(),
        attempt_no=1,
    )
    new_row = make_orchestration(
        orchestration_id=_RETRY_ID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        status=OrchestrationStatus.PENDING.value,
        input_fingerprint=_stored_fp(),
        attempt_no=2,
        retry_of_orchestration_id=_OID,
    )
    created_rows: list = []

    async def fake_get(self, orchestration_id):
        return old

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    async def fake_get_for_update(self, orchestration_id):
        return old

    async def fake_get_latest(self, research_plan_id):
        return old  # max attempt = 1 → new_attempt = 2

    async def fake_create_or_get(self, orchestration):
        assert orchestration.orchestration_id != _OID
        assert orchestration.task_id == _TASK_ID
        assert orchestration.research_plan_id == _PLAN_ID
        assert orchestration.attempt_no == 2
        assert orchestration.retry_of_orchestration_id == _OID
        assert orchestration.input_fingerprint == _stored_fp()
        assert orchestration.status == OrchestrationStatus.PENDING.value
        assert orchestration.current_phase == "planning"
        created_rows.append(orchestration)
        return new_row, True

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_id_for_update", fake_get_for_update
    )
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_plan", fake_get_latest)
    monkeypatch.setattr(ResearchOrchestrationRepository, "create_or_get", fake_create_or_get)

    result = await _service(sessionmaker).retry_orchestration(_OID)
    assert result.replayed is False
    assert result.orchestration_id == _RETRY_ID
    assert result.attempt_no == 2
    assert result.retry_of_orchestration_id == _OID
    assert len(created_rows) == 1
    # old 行未被修改（fake 只读；断言值未变）。
    assert old.attempt_no == 1
    assert old.retry_of_orchestration_id is None


async def test_retry_concurrent_returns_existing_attempt(monkeypatch) -> None:
    """并发 retry（同 old）：latest.attempt > old.attempt 且 retry_of=old →
    返回 winner，不再创建 attempt=2。"""
    sessionmaker = FakeSessionMaker()
    old = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        status=OrchestrationStatus.FAILED.value,
        input_fingerprint=_stored_fp(),
        attempt_no=1,
    )
    winner = make_orchestration(
        orchestration_id=_RETRY_ID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        status=OrchestrationStatus.PENDING.value,
        input_fingerprint=_stored_fp(),
        attempt_no=2,
        retry_of_orchestration_id=_OID,
    )

    async def fake_get(self, orchestration_id):
        return old

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    async def fake_get_for_update(self, orchestration_id):
        return old

    async def fake_get_latest(self, research_plan_id):
        return winner

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_id_for_update", fake_get_for_update
    )
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_plan", fake_get_latest)

    result = await _service(sessionmaker).retry_orchestration(_OID)
    assert result.replayed is True
    assert result.orchestration_id == winner.orchestration_id
    assert result.attempt_no == 2


async def test_retry_rejects_completed(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    old = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        status=OrchestrationStatus.COMPLETED.value,
        input_fingerprint=_stored_fp(),
    )

    async def fake_get(self, orchestration_id):
        return old

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    async def fake_get_for_update(self, orchestration_id):
        return old

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_id_for_update", fake_get_for_update
    )
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await _service(sessionmaker).retry_orchestration(_OID)


async def test_retry_rejects_active(monkeypatch) -> None:
    """active（running）→ 拒绝（不是 failed/cancelled）。"""
    sessionmaker = FakeSessionMaker()
    old = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        status=OrchestrationStatus.RUNNING.value,
        input_fingerprint=_stored_fp(),
    )

    async def fake_get(self, orchestration_id):
        return old

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    async def fake_get_for_update(self, orchestration_id):
        return old

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    monkeypatch.setattr(
        ResearchOrchestrationRepository, "get_by_id_for_update", fake_get_for_update
    )
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await _service(sessionmaker).retry_orchestration(_OID)


async def test_retry_missing_orchestration(monkeypatch) -> None:
    """orchestration 不存在 → NotFound（verify 阶段）。"""
    sessionmaker = FakeSessionMaker()

    async def fake_get(self, orchestration_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationNotFound):
        await _service(sessionmaker).retry_orchestration(_OID)


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


async def test_verify_integrity_retry_parent_ok(monkeypatch) -> None:
    """retry row 的 retry_of 同 task/plan → 通过（spec B）。"""
    sessionmaker = FakeSessionMaker()
    stored_fp = _stored_fp()
    parent = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        input_fingerprint=stored_fp,
        attempt_no=1,
    )
    row = make_orchestration(
        orchestration_id=_RETRY_ID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        input_fingerprint=stored_fp,
        attempt_no=2,
        retry_of_orchestration_id=_OID,
    )

    async def fake_get(self, orchestration_id):
        if orchestration_id == row.orchestration_id:
            return row
        return parent

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    result = await _service(sessionmaker).verify_orchestration_integrity(row.orchestration_id)
    assert result is row


async def test_verify_integrity_retry_parent_task_mismatch(monkeypatch) -> None:
    """retry_of 与行同 plan 但 task 不同 → integrity error（spec B）。"""
    sessionmaker = FakeSessionMaker()
    stored_fp = _stored_fp()
    row = make_orchestration(
        orchestration_id=_RETRY_ID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        input_fingerprint=stored_fp,
        attempt_no=2,
        retry_of_orchestration_id=_OID,
    )
    parent = make_orchestration(
        orchestration_id=_OID,
        task_id=UUID("00000000-0000-0000-0000-00000000000b"),
        research_plan_id=_PLAN_ID,
    )

    async def fake_get(self, orchestration_id):
        if orchestration_id == row.orchestration_id:
            return row
        return parent

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    with pytest.raises(ResearchOrchestrationIntegrityError):
        await _service(sessionmaker).verify_orchestration_integrity(row.orchestration_id)


async def test_verify_integrity_retry_parent_missing(monkeypatch) -> None:
    """retry_of 指向不存在的 orchestration → integrity error。"""
    sessionmaker = FakeSessionMaker()
    stored_fp = _stored_fp()
    row = make_orchestration(
        orchestration_id=_RETRY_ID,
        task_id=_TASK_ID,
        research_plan_id=_PLAN_ID,
        input_fingerprint=stored_fp,
        attempt_no=2,
        retry_of_orchestration_id=_OID,
    )

    async def fake_get(self, orchestration_id):
        if orchestration_id == row.orchestration_id:
            return row
        return None

    async def fake_plan_get(self, research_plan_id):
        return _plan_stub()

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchPlanRepository, "get_by_id", fake_plan_get)
    with pytest.raises(ResearchOrchestrationIntegrityError):
        await _service(sessionmaker).verify_orchestration_integrity(row.orchestration_id)


# ------------------------------------------------------------- start / current（7A.2B.2 spec U）


def _result(
    *,
    status: str = "pending",
    current_phase: str = "planning",
    orchestration_id=_OID,
    attempt_no: int = 1,
    retry_of_orchestration_id: UUID | None = None,
):
    return ResearchOrchestrationService._to_result(
        make_orchestration(
            orchestration_id=orchestration_id,
            task_id=_TASK_ID,
            status=status,
            current_phase=current_phase,
            attempt_no=attempt_no,
            retry_of_orchestration_id=retry_of_orchestration_id,
        )
    )


async def test_prepare_start_first_ever_creates_attempt1_and_schedules(monkeypatch) -> None:
    """Case 1：task 从未有 orchestration → create attempt1 + schedule O1（201/202）。"""
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        FakeSessionMaker(), FakePlanService(), execution_manager=manager
    )
    created = _result()

    async def fake_active(self, task_id):
        return None

    async def fake_latest(self, task_id):
        return None

    async def fake_create(self, task_id):
        return created

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)
    monkeypatch.setattr(ResearchOrchestrationService, "create_or_get_orchestration", fake_create)

    outcome = await service.prepare_orchestration_start(_TASK_ID)
    assert outcome.created is True
    assert outcome.scheduled is True
    assert outcome.orchestration.orchestration_id == _OID
    assert manager.scheduled == [_OID]


async def test_prepare_start_active_pending_schedules_exact_active(monkeypatch) -> None:
    """Case 2：active=pending → 返回 exact active；本进程无 local task → schedule。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="pending", current_phase="planning"
    )

    async def fake_active(self, task_id):
        return active

    async def fake_latest(self, task_id):
        raise AssertionError("get_latest must not be called when active exists")

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)

    outcome = await service.prepare_orchestration_start(_TASK_ID)
    assert outcome.created is False
    assert outcome.scheduled is True
    assert outcome.orchestration.orchestration_id == _OID
    assert manager.scheduled == [_OID]


async def test_prepare_start_active_running_no_schedule(monkeypatch) -> None:
    """Case 3：active=running → 返回 active，不重复 schedule。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)

    outcome = await service.prepare_orchestration_start(_TASK_ID)
    assert outcome.created is False
    assert outcome.scheduled is False
    assert manager.scheduled == []


async def test_prepare_start_active_waiting_human_no_auto_resume(monkeypatch) -> None:
    """Case 4：active=waiting_human → 返回 active，不自动 resume / schedule。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="awaiting_stage5",
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)

    outcome = await service.prepare_orchestration_start(_TASK_ID)
    assert outcome.created is False
    assert outcome.scheduled is False
    assert manager.scheduled == []
    assert manager.cancelled == []


async def test_prepare_start_latest_completed_returns_completed(monkeypatch) -> None:
    """Case 5：无 active、latest=completed → 返回 latest completed（不 create / schedule）。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    completed = make_orchestration(
        orchestration_id=_RETRY_ID,
        task_id=_TASK_ID,
        status="completed",
        current_phase="completed",
        attempt_no=2,
    )

    async def fake_active(self, task_id):
        return None

    async def fake_latest(self, task_id):
        return completed

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)

    outcome = await service.prepare_orchestration_start(_TASK_ID)
    assert outcome.created is False
    assert outcome.scheduled is False
    assert outcome.orchestration.orchestration_id == _RETRY_ID
    assert outcome.orchestration.status == "completed"
    assert manager.scheduled == []


async def test_prepare_start_latest_failed_raises_retry_required(monkeypatch) -> None:
    """Case 6：无 active、latest=failed → 409 retry_required（不偷偷回 attempt1）。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    failed = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="failed", current_phase="stage4"
    )

    async def fake_active(self, task_id):
        return None

    async def fake_latest(self, task_id):
        return failed

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)

    with pytest.raises(ResearchOrchestrationRetryRequired):
        await service.prepare_orchestration_start(_TASK_ID)
    assert manager.scheduled == []


# ------------------------------------------------------------ retry + schedule（Gate E）


async def test_retry_and_schedule_creates_o2_and_schedules(monkeypatch) -> None:
    """retry HTTP → 创建 O2 + 自动 schedule（不需要第二次 API 调用再 start O2）。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    o2 = _result(
        orchestration_id=_RETRY_ID, status="pending", current_phase="planning", attempt_no=2
    )

    async def fake_retry(self, orchestration_id):
        return o2

    monkeypatch.setattr(ResearchOrchestrationService, "retry_orchestration", fake_retry)
    result = await service.retry_and_schedule(_OID)
    assert result.orchestration_id == _RETRY_ID
    assert result.attempt_no == 2
    assert manager.scheduled == [_RETRY_ID]


async def test_retry_and_schedule_without_manager_only_creates(monkeypatch) -> None:
    """manager 未绑定 → retry_and_schedule 只创建 O2，不调度（unit 测试场景）。"""
    sessionmaker = FakeSessionMaker()
    service = ResearchOrchestrationService(sessionmaker, FakePlanService())
    o2 = _result(orchestration_id=_RETRY_ID)

    async def fake_retry(self, orchestration_id):
        return o2

    monkeypatch.setattr(ResearchOrchestrationService, "retry_orchestration", fake_retry)
    result = await service.retry_and_schedule(_OID)
    assert result.orchestration_id == _RETRY_ID


# ------------------------------------------------------------ cancel（Gate F）


async def test_cancel_cancels_local_task_then_db(monkeypatch) -> None:
    """API cancel → 先协作式取消本地 task，再 DB cancelled（不出现 DB 仍 running）。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )
    manager.schedule(_OID)  # 模拟本进程已有 local task

    async def fake_get(self, orchestration_id):
        return active

    async def fake_list_children(self, orchestration_id):
        return []

    async def fake_orch_mark_cancelled(self, orchestration_id, completed_at):
        return make_orchestration(status=OrchestrationStatus.CANCELLED.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "list_children", fake_list_children)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_cancelled", fake_orch_mark_cancelled)

    result = await service.cancel_orchestration(_OID)
    assert result.status == OrchestrationStatus.CANCELLED.value
    assert manager.cancelled == [_OID]
    assert manager.is_scheduled(_OID) is False


async def test_cancel_no_local_task_is_noop(monkeypatch) -> None:
    """cancel 时无 local task → cancel_local 不记录（已运行/其他进程）。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), execution_manager=manager
    )
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )

    async def fake_get(self, orchestration_id):
        return active

    async def fake_list_children(self, orchestration_id):
        return []

    async def fake_orch_mark_cancelled(self, orchestration_id, completed_at):
        return make_orchestration(status=OrchestrationStatus.CANCELLED.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "list_children", fake_list_children)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_cancelled", fake_orch_mark_cancelled)

    # 无 live task（未 schedule）→ cancel_local no-op。
    result = await service.cancel_orchestration(_OID)
    assert result.status == OrchestrationStatus.CANCELLED.value
    assert manager.cancelled == []


async def test_get_current_prefers_active(monkeypatch) -> None:
    """active orchestration 存在 → 返回 active（不回落 latest）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )
    latest = make_orchestration(
        orchestration_id=_RETRY_ID, task_id=_TASK_ID, status="failed", current_phase="stage5"
    )

    async def fake_active(self, task_id):
        return active

    async def fake_latest(self, task_id):
        return latest

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)

    result = await _service(sessionmaker).get_current_orchestration(_TASK_ID)
    assert result.orchestration_id == _OID


async def test_get_current_falls_back_to_latest(monkeypatch) -> None:
    """无 active → 返回最近一条（含 terminal history）。"""
    sessionmaker = FakeSessionMaker()
    latest = make_orchestration(
        orchestration_id=_RETRY_ID, task_id=_TASK_ID, status="failed", current_phase="stage5"
    )

    async def fake_active(self, task_id):
        return None

    async def fake_latest(self, task_id):
        return latest

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)

    result = await _service(sessionmaker).get_current_orchestration(_TASK_ID)
    assert result.orchestration_id == _RETRY_ID


async def test_get_current_none_raises_not_found(monkeypatch) -> None:
    """task 无任何 orchestration → 404。"""
    sessionmaker = FakeSessionMaker()

    async def fake_active(self, task_id):
        return None

    async def fake_latest(self, task_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    monkeypatch.setattr(ResearchOrchestrationRepository, "get_latest_for_task", fake_latest)

    with pytest.raises(ResearchOrchestrationNotFound):
        await _service(sessionmaker).get_current_orchestration(_TASK_ID)


# ------------------------------------- checkpoint projection（7A Product Gate spec O）


class _ProjectionRunner:
    """fake orchestration runner：`read_orchestration_checkpoint` 返回指定 dict。"""

    def __init__(
        self,
        checkpoint: dict | None = None,
        *,
        fail_with: BaseException | None = None,
    ) -> None:
        self.checkpoint = checkpoint if checkpoint is not None else {}
        self.fail_with = fail_with
        self.read_calls: list[UUID] = []

    async def read_orchestration_checkpoint(self, orchestration_id: UUID) -> dict:
        self.read_calls.append(orchestration_id)
        if self.fail_with is not None:
            raise self.fail_with
        return self.checkpoint


async def test_projection_unbound_runner_returns_none_derived_fields(monkeypatch) -> None:
    """runner 未绑定（unit 测试场景）→ checkpoint 派生字段全 None（纯 row 投影）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    result = await _service(sessionmaker).get_current_orchestration(_TASK_ID)
    assert result.orchestration_id == _OID
    assert result.current_child_run_id is None
    assert result.backflow_round is None
    assert result.research_request_id is None
    assert result.manual_reason is None
    assert result.missing_need_codes is None
    assert result.updated_at is None


async def test_projection_merges_checkpoint_when_runner_bound(monkeypatch) -> None:
    """runner 绑定 + checkpoint 有值 → 投影 checkpoint 派生字段（str→UUID 转换）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="research_backflow",
    )
    runner = _ProjectionRunner(
        {
            "current_child_run_id": "00000000-0000-0000-0000-000000000009",
            "backflow_round": 1,
            "research_request_id": "00000000-0000-0000-0000-00000000000a",
            "backflow_manual_reason": "structured_data_refresh_required",
            "missing_need_codes": ["unsupported_by_evidence", "weak_source_quality"],
        }
    )
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), orchestration_runner=runner
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    result = await service.get_current_orchestration(_TASK_ID)
    assert runner.read_calls == [_OID]
    assert result.status == "waiting_human"
    assert result.current_child_run_id == UUID("00000000-0000-0000-0000-000000000009")
    assert result.backflow_round == 1
    assert result.research_request_id == UUID("00000000-0000-0000-0000-00000000000a")
    assert result.manual_reason == "structured_data_refresh_required"
    assert result.missing_need_codes == ["unsupported_by_evidence", "weak_source_quality"]


async def test_projection_invalid_child_run_id_is_none(monkeypatch) -> None:
    """checkpoint 的 current_child_run_id 非法 → None（`_uuid_or_none` 兜底）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="research_backflow"
    )
    runner = _ProjectionRunner({"current_child_run_id": "not-a-uuid", "backflow_round": 2})
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), orchestration_runner=runner
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    result = await service.get_current_orchestration(_TASK_ID)
    assert result.current_child_run_id is None
    assert result.backflow_round == 2


async def test_projection_checkpoint_read_failure_falls_back_to_row(monkeypatch) -> None:
    """checkpoint 读取异常 → 不降级为错误；派生字段保持 None（row 权威）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )
    runner = _ProjectionRunner(fail_with=RuntimeError("checkpoint down"))
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), orchestration_runner=runner
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    result = await service.get_current_orchestration(_TASK_ID)
    assert result.orchestration_id == _OID
    assert result.status == "running"
    assert result.current_child_run_id is None
    assert result.backflow_round is None
    assert result.research_request_id is None
    assert result.manual_reason is None


async def test_projection_updated_at_from_row(monkeypatch) -> None:
    """updated_at 取 row（不来自 checkpoint），tz-aware 投影。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )
    active.updated_at = datetime.now(UTC)
    runner = _ProjectionRunner({})
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), orchestration_runner=runner
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)
    result = await service.get_current_orchestration(_TASK_ID)
    assert result.updated_at is not None
    assert result.updated_at.tzinfo is not None


async def test_projection_merges_into_prepare_start_waiting_human(monkeypatch) -> None:
    """prepare_orchestration_start Case 4（waiting_human）→ 投影 checkpoint 派生字段
    （前端 phase banner / 补资料 UI 的关键路径）。"""
    sessionmaker = FakeSessionMaker()
    manager = FakeExecutionManager()
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="research_backflow",
    )
    runner = _ProjectionRunner(
        {
            "backflow_manual_reason": "source_acquisition_required",
            "missing_need_codes": ["unsupported_by_evidence"],
        }
    )
    service = ResearchOrchestrationService(
        sessionmaker, FakePlanService(), orchestration_runner=runner, execution_manager=manager
    )

    async def fake_active(self, task_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_active_for_task", fake_active)

    outcome = await service.prepare_orchestration_start(_TASK_ID)
    assert outcome.created is False
    assert outcome.scheduled is False
    assert outcome.orchestration.manual_reason == "source_acquisition_required"
    assert outcome.orchestration.missing_need_codes == ["unsupported_by_evidence"]


# ------------------- resume after source acquisition（7A Product Gate spec J/K/L）


def _resume_service(runner, manager) -> ResearchOrchestrationService:
    """绑定 runner（checkpoint 读取）+ manager（schedule_resume 记录）的 service。"""
    return ResearchOrchestrationService(
        FakeSessionMaker(),
        FakePlanService(),
        orchestration_runner=runner,
        execution_manager=manager,
    )


async def test_resume_waiting_manual_schedules_prepare(monkeypatch) -> None:
    """K1：waiting_manual（补资料后）→ schedule_resume(kind=prepare) 后台恢复。"""
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="waiting_manual",
    )
    runner = _ProjectionRunner({"current_phase": "waiting_manual"})
    manager = FakeExecutionManager()
    service = _resume_service(runner, manager)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    result = await service.resume_after_source_acquisition(_OID)
    assert manager.resumed == [(_OID, RESUME_KIND_PREPARE)]
    assert result.orchestration_id == _OID


async def test_resume_research_backflow_source_required_schedules_supplemental(monkeypatch) -> None:
    """K2：research_backflow + reason=source_acquisition_required →
    schedule_resume(kind=supplemental_research)（同 round 重跑补充研究）。"""
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="research_backflow",
    )
    runner = _ProjectionRunner(
        {
            "current_phase": "research_backflow",
            "backflow_manual_reason": "source_acquisition_required",
        }
    )
    manager = FakeExecutionManager()
    service = _resume_service(runner, manager)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    result = await service.resume_after_source_acquisition(_OID)
    assert manager.resumed == [(_OID, RESUME_KIND_SUPPLEMENTAL_RESEARCH)]
    assert result.orchestration_id == _OID


async def test_resume_structured_refresh_rejected(monkeypatch) -> None:
    """D2：reason=structured_data_refresh_required **不在** RESUME_BACKFLOW_MANUAL_
    REASONS → InvalidAction 稳定拒绝（结构化 refresh 不在 automatic 文档补充研究
    范围，补 PDF / URL 不能解决）；不调度。"""
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="research_backflow",
    )
    runner = _ProjectionRunner(
        {
            "current_phase": "research_backflow",
            "backflow_manual_reason": "structured_data_refresh_required",
        }
    )
    manager = FakeExecutionManager()
    service = _resume_service(runner, manager)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationInvalidAction):
        await service.resume_after_source_acquisition(_OID)
    assert manager.resumed == []


async def test_resume_limit_reached_rejected(monkeypatch) -> None:
    """K3：reason=research_backflow_limit_reached → InvalidAction（MAX rounds 不可
    绕过，须 retry 新 orchestration）；不调度。"""
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="research_backflow",
    )
    runner = _ProjectionRunner(
        {
            "current_phase": "research_backflow",
            "backflow_manual_reason": "research_backflow_limit_reached",
        }
    )
    manager = FakeExecutionManager()
    service = _resume_service(runner, manager)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationInvalidAction):
        await service.resume_after_source_acquisition(_OID)
    assert manager.resumed == []


async def test_resume_awaiting_stage5_rejected(monkeypatch) -> None:
    """L：phase=awaiting_stage5（Stage5 人工裁决）→ InvalidAction，**与
    HumanReviewDecision 分开**（走 act_on_orchestration / /actions）；不调度。"""
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="awaiting_stage5",
    )
    runner = _ProjectionRunner({"current_phase": "awaiting_stage5"})
    manager = FakeExecutionManager()
    service = _resume_service(runner, manager)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationInvalidAction):
        await service.resume_after_source_acquisition(_OID)
    assert manager.resumed == []


async def test_resume_no_progress_rejected(monkeypatch) -> None:
    """reason=research_backflow_no_progress（genuine no-progress，非缺资料）→
    不可恢复（InvalidAction）。"""
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="research_backflow",
    )
    runner = _ProjectionRunner(
        {
            "current_phase": "research_backflow",
            "backflow_manual_reason": "research_backflow_no_progress",
        }
    )
    manager = FakeExecutionManager()
    service = _resume_service(runner, manager)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationInvalidAction):
        await service.resume_after_source_acquisition(_OID)
    assert manager.resumed == []


async def test_resume_not_waiting_human_rejected(monkeypatch) -> None:
    """status ≠ waiting_human（running/completed…）→ AlreadyFinished（与 action 一致）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID, task_id=_TASK_ID, status="running", current_phase="stage4"
    )
    service = _service(sessionmaker)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await service.resume_after_source_acquisition(_OID)


async def test_resume_missing_orchestration_raises_not_found(monkeypatch) -> None:
    service = _service(FakeSessionMaker())

    async def fake_get(self, orchestration_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationNotFound):
        await service.resume_after_source_acquisition(_OID)


async def test_resume_unbound_runners_raises_runtime_error(monkeypatch) -> None:
    """runner / manager 未绑定（unit 场景）→ RuntimeError（production factory 绑定）。"""
    sessionmaker = FakeSessionMaker()
    active = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status="waiting_human",
        current_phase="waiting_manual",
    )
    service = _service(sessionmaker)

    async def fake_get(self, orchestration_id):
        return active

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(RuntimeError):
        await service.resume_after_source_acquisition(_OID)
