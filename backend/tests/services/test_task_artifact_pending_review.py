"""P0/P2 一致性修复：无 report_audit 行时 pending_human_review 投影的单元测试。

只读投影后台真实 orchestration + backflow request/decision 状态（0 LLM，复用
Fake 单测 harness）。核心不变量：**绝不在没有真实人工等待时伪造 pending 投影**——
orchestration 非 waiting_human / 非人工复核 phase / 无 backflow request 时
必须返回 None（Reviews 页此时才显示「无审核记录」，这才是合理状态）。
"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

import app.services.task_artifact_service as svc_mod
from app.research_orchestration.contracts import (
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.schemas.artifact import PendingHumanReviewArtifact


class FakeRequester:
    """fake repo：返回预设 orchestration 行。"""

    def __init__(self, orchestration):
        self._orchestration = orchestration

    async def get_latest_for_task(self, task_id):
        return self._orchestration


class FakeClosureResult:
    def __init__(
        self,
        *,
        reason: str = "report_check_unavailable",
        decision: str | None = None,
        comment: str | None = None,
        decided_at=None,
    ):
        self.reason = reason
        self.decision = decision
        self.comment = comment
        self.decided_at = decided_at


class FakeClosure:
    def __init__(
        self,
        *,
        request_reason: str | None = None,
        request_id: UUID | None = None,
        decision: str | None = None,
        comment: str | None = None,
        decided_at=None,
    ):
        self._request = (
            FakeRequest(req_id=request_id, reason=request_reason)
            if request_reason is not None
            else None
        )
        self._decision = (
            FakeDecision(decision=decision, comment=comment, decided_at=decided_at)
            if decision is not None
            else None
        )

    async def get_request_for_orchestration(self, orchestration_id):
        return self._request

    async def get_decision_for_request(self, request_id):
        return self._decision


class FakeRequest:
    def __init__(self, *, req_id, reason):
        self.backflow_human_request_id = req_id or uuid4()
        self.reason = reason


class FakeDecision:
    def __init__(self, *, decision, comment, decided_at):
        self.decision = decision
        self.comment = comment
        self.decided_at = decided_at


class FakeOrchestration:
    def __init__(self, *, status, phase):
        self.status = status
        self.current_phase = phase
        self.orchestration_id = uuid4()


def _build_service(monkeypatch, *, orchestration, closure):
    """构造未实例化的 TaskArtifactService，patch repo + closure 工厂。"""

    class Repo:
        def __init__(self, session):
            self._session = session

        async def get_latest_for_task(self, task_id):
            return orchestration

    monkeypatch.setattr(svc_mod, "ResearchOrchestrationRepository", Repo)
    monkeypatch.setattr(svc_mod, "ResearchBackflowClosureService", lambda sm: closure)

    class FakeSessionMaker:
        def __call__(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    svc = object.__new__(svc_mod.TaskArtifactService)
    svc._sessionmaker = FakeSessionMaker()
    return svc


@pytest.mark.asyncio
async def test_returns_pending_when_waiting_manual_backflow(monkeypatch) -> None:
    """waiting_human + research_backflow + 有 request → 返回真实 pending 投影。"""
    orch = FakeOrchestration(
        status=OrchestrationStatus.WAITING_HUMAN.value,
        phase=OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    closure = FakeClosure(
        request_reason="report_audit_unavailable",
        decision="extra_research",
        comment="请补充资料",
        decided_at=datetime(2026, 8, 11, 10, 0, tzinfo=UTC),
    )
    svc = _build_service(monkeypatch, orchestration=orch, closure=closure)
    pending = await svc_mod.TaskArtifactService._resolve_pending_manual_review(svc, uuid4())
    assert isinstance(pending, PendingHumanReviewArtifact)
    assert pending.reason == "report_audit_unavailable"
    assert pending.decision == "extra_research"
    assert pending.comment == "请补充资料"
    assert pending.decided_at == datetime(2026, 8, 11, 10, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_none_when_not_waiting_human(monkeypatch) -> None:
    """非 waiting_human → None（后台并未等待人工，Reviews 显示无记录是合理状态）。"""
    orch = FakeOrchestration(
        status=OrchestrationStatus.RUNNING.value,
        phase=OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    closure = FakeClosure(request_reason="report_audit_unavailable")
    svc = _build_service(monkeypatch, orchestration=orch, closure=closure)
    pending = await svc_mod.TaskArtifactService._resolve_pending_manual_review(svc, uuid4())
    assert pending is None


@pytest.mark.asyncio
async def test_none_when_phase_not_manual(monkeypatch) -> None:
    """waiting_human 但 phase 非 research_backflow/awaiting_stage5 → None。"""
    orch = FakeOrchestration(
        status=OrchestrationStatus.WAITING_HUMAN.value,
        phase=OrchestrationPhase.STAGE5.value,
    )
    closure = FakeClosure(request_reason="report_audit_unavailable")
    svc = _build_service(monkeypatch, orchestration=orch, closure=closure)
    pending = await svc_mod.TaskArtifactService._resolve_pending_manual_review(svc, uuid4())
    assert pending is None


@pytest.mark.asyncio
async def test_none_when_no_backflow_request(monkeypatch) -> None:
    """waiting_human + research_backflow 但无 request 行 → None（真实后台异常态，
    投影不伪造）。"""
    orch = FakeOrchestration(
        status=OrchestrationStatus.WAITING_HUMAN.value,
        phase=OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    closure = FakeClosure()  # no request
    svc = _build_service(monkeypatch, orchestration=orch, closure=closure)
    pending = await svc_mod.TaskArtifactService._resolve_pending_manual_review(svc, uuid4())
    assert pending is None
