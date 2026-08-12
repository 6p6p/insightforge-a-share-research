"""ResearchOrchestrationRecoveryCoordinator unit tests（spec O/E + 7A.2B.2 Q，0 DB）。

`_recover_one` 判定：
- terminal / None → 不恢复；
- `awaiting_stage5`（正常 terminal pause，等 Stage5 人工裁决）→ 不恢复；
- phase=stage4 / stage5 且 exact child `running`（live executor / rolling
  restart）→ 跳过（stage5 必须跳过：`_stage5_outcome` 对 running child 抛
  IntegrityError，不跳过会误标 failed）；
- phase=stage4 / stage5 且 child failed(worker_restarted) / waiting_human /
  missing → 恢复顶层 graph（`run_or_resume_stage4` / `run_or_resume_stage5`
  节点做 execute / resume / 跳过 collect）。
绝不新建 orchestration / 绝不换 thread（同 orchestration_id + 同顶层 thread）。
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import OrchestrationPhase, OrchestrationStatus
from app.research_orchestration.recovery import ResearchOrchestrationRecoveryCoordinator
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from tests.research_orchestration.fakes import (
    FakeRecoveryRunner,
    FakeSessionMaker,
    make_orchestration,
)

pytestmark = pytest.mark.asyncio

_OID = UUID("00000000-0000-0000-0000-000000000001")


def _coordinator(sessionmaker, runner) -> ResearchOrchestrationRecoveryCoordinator:
    return ResearchOrchestrationRecoveryCoordinator(sessionmaker, runner)


def _orchestration_row(*, status: str = "running", current_phase: str = "stage4"):
    return make_orchestration(status=status, current_phase=current_phase)


async def test_terminal_skip(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner()

    async def fake_get(self, orchestration_id):
        return _orchestration_row(status=OrchestrationStatus.FAILED.value)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []


async def test_awaiting_stage5_skip(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner()

    async def fake_get(self, orchestration_id):
        return _orchestration_row(
            status=OrchestrationStatus.RUNNING.value,
            current_phase=OrchestrationPhase.AWAITING_STAGE5.value,
        )

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []


async def test_research_backflow_waiting_human_skip(monkeypatch) -> None:
    """7A.2B.3：research_backflow（waiting_human）等人工裁决，不自动恢复。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner()

    async def fake_get(self, orchestration_id):
        return _orchestration_row(
            status=OrchestrationStatus.WAITING_HUMAN.value,
            current_phase=OrchestrationPhase.RESEARCH_BACKFLOW.value,
        )

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []


async def test_waiting_manual_skip(monkeypatch) -> None:
    """waiting_manual（waiting_human）等人工，不自动恢复。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner()

    async def fake_get(self, orchestration_id):
        return _orchestration_row(
            status=OrchestrationStatus.WAITING_HUMAN.value,
            current_phase=OrchestrationPhase.WAITING_MANUAL.value,
        )

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []


async def test_stage4_child_running_skip(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE4.value)

    async def fake_get(self, orchestration_id):
        return _orchestration_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return SimpleNamespace(workflow_run_id=UUID("00000000-0000-0000-0000-000000000009"))

    async def fake_run_get(self, run_id):
        return SimpleNamespace(
            run_id=UUID("00000000-0000-0000-0000-000000000009"), status="running"
        )

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []


async def test_stage4_child_failed_resumes(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE4.value)

    async def fake_get(self, orchestration_id):
        return _orchestration_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        assert stage == "stage4"
        return SimpleNamespace(workflow_run_id=UUID("00000000-0000-0000-0000-000000000009"))

    async def fake_run_get(self, run_id):
        return SimpleNamespace(status="failed")  # FAILED(worker_restarted) 可恢复

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is True
    assert runner.run_calls == [_OID]  # 同 orchestration_id + 同顶层 thread


async def test_stage4_no_child_resumes(monkeypatch) -> None:
    """crash 在 ensure_stage4_child 完成前：无 child → 顶层 graph 重新 ensure/execute。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE4.value)

    async def fake_get(self, orchestration_id):
        return _orchestration_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is True
    assert runner.run_calls == [_OID]


# ------------------------------------------------------------------ stage5 (7A.2B.2 Q)


def _stage5_row() -> SimpleNamespace:
    return _orchestration_row(current_phase=OrchestrationPhase.STAGE5.value)


async def test_stage5_child_running_skip(monkeypatch) -> None:
    """phase=stage5 且 stage5 child 仍 running（live executor / rolling restart）→
    跳过。必须跳过：`_stage5_outcome` 对 running child 抛 IntegrityError，不跳过
    会把有 live executor 的 orchestration 误标 failed。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE5.value)

    async def fake_get(self, orchestration_id):
        return _stage5_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        assert stage == "stage5"
        return SimpleNamespace(workflow_run_id=UUID("00000000-0000-0000-0000-000000000009"))

    async def fake_run_get(self, run_id):
        return SimpleNamespace(
            run_id=UUID("00000000-0000-0000-0000-000000000009"), status="running"
        )

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []


async def test_stage5_child_failed_resumes(monkeypatch) -> None:
    """phase=stage5 且 child failed(worker_restarted) → 恢复顶层 graph，由
    run_or_resume_stage5 节点 resume_stage5_for_recovery。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE5.value)

    async def fake_get(self, orchestration_id):
        return _stage5_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        assert stage == "stage5"
        return SimpleNamespace(workflow_run_id=UUID("00000000-0000-0000-0000-000000000009"))

    async def fake_run_get(self, run_id):
        return SimpleNamespace(status="failed")

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is True
    assert runner.run_calls == [_OID]


async def test_stage5_child_waiting_human_resumes(monkeypatch) -> None:
    """phase=stage5 且 child waiting_human（人工已裁决 / crash 在 awaiting_stage5
    持久化前）→ 恢复顶层 graph 收敛到 awaiting_stage5。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE5.value)

    async def fake_get(self, orchestration_id):
        return _stage5_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        assert stage == "stage5"
        return SimpleNamespace(workflow_run_id=UUID("00000000-0000-0000-0000-000000000009"))

    async def fake_run_get(self, run_id):
        return SimpleNamespace(status="waiting_human")

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)
    monkeypatch.setattr(WorkflowRunRepository, "get_by_id", fake_run_get)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is True
    assert runner.run_calls == [_OID]


async def test_stage5_no_child_resumes(monkeypatch) -> None:
    """crash 在 ensure_stage5_child 完成前：无 stage5 child → 顶层 graph 重新
    ensure / execute（run_or_resume_stage5 节点判定）。"""
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner(checkpoint_phase=OrchestrationPhase.STAGE5.value)

    async def fake_get(self, orchestration_id):
        return _stage5_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        assert stage == "stage5"
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is True
    assert runner.run_calls == [_OID]


async def test_missing_orchestration_returns_false(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()
    runner = FakeRecoveryRunner()

    async def fake_get(self, orchestration_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    ok = await _coordinator(sessionmaker, runner)._recover_one(_OID)
    assert ok is False
    assert runner.run_calls == []
