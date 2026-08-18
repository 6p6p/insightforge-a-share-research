"""P0 backflow manual closure unit tests (no DB).

Covers: fingerprint determinism, comment normalization, acceptance guard
(accept enabled / disabled), extra_research bounded round scheduling, cancel
clean terminal, and closure view barriers -- all with injected fakes (0 DB).
"""

from __future__ import annotations

import uuid

import pytest

from app.research_backflow.closure import (
    BACKFLOW_DECISION_ACCEPT,
    BACKFLOW_DECISION_CANCEL,
    BACKFLOW_DECISION_EXTRA_RESEARCH,
    BackflowReviewNotAcceptable,
    compute_backflow_decision_fingerprint,
    compute_backflow_review_fingerprint,
    normalize_backflow_comment,
)
from app.research_orchestration.errors import ResearchOrchestrationAlreadyFinished
from app.research_orchestration.service import ResearchOrchestrationService

# ------------------------------------------------------------------ pure functions


def test_review_fingerprint_deterministic_and_sensitive():
    orch_id = uuid.uuid4()
    fp1 = compute_backflow_review_fingerprint(
        request_schema_version=1,
        orchestration_id=orch_id,
        reason="research_backflow_limit_reached",
        request_payload={"backflow_round": 2},
    )
    fp2 = compute_backflow_review_fingerprint(
        request_schema_version=1,
        orchestration_id=orch_id,
        reason="research_backflow_limit_reached",
        request_payload={"backflow_round": 2},
    )
    assert fp1 == fp2
    fp3 = compute_backflow_review_fingerprint(
        request_schema_version=1,
        orchestration_id=orch_id,
        reason="research_backflow_no_progress",
        request_payload={"backflow_round": 2},
    )
    assert fp1 != fp3


def test_decision_fingerprint_deterministic():
    req_id = uuid.uuid4()
    fp = compute_backflow_decision_fingerprint(
        decision_schema_version=1,
        backflow_human_request_id=req_id,
        request_fingerprint="a" * 64,
        decision=BACKFLOW_DECISION_ACCEPT,
        comment=None,
    )
    assert len(fp) == 64
    assert fp == compute_backflow_decision_fingerprint(
        decision_schema_version=1,
        backflow_human_request_id=req_id,
        request_fingerprint="a" * 64,
        decision=BACKFLOW_DECISION_ACCEPT,
        comment=None,
    )


def test_normalize_comment():
    assert normalize_backflow_comment(None) is None
    assert normalize_backflow_comment("  ") is None
    assert normalize_backflow_comment("  已确认  ") == "已确认"


# ------------------------------------------------------------------ fakes


class FakeSession:
    """占位 session（repo 被 patch，session 只作为参数传递）。"""

    async def commit(self):
        return None


class _FakeAsyncCM:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc):
        return False


class FakeSessionmaker:
    def __init__(self):
        self.session = FakeSession()

    def __call__(self):
        return _FakeAsyncCM(self.session)


class FakeOrchestrationRow:
    def __init__(self, *, status="waiting_human", phase="research_backflow"):
        self.status = status
        self.current_phase = phase


class FakeClosureRequest:
    def __init__(self, request_id):
        self.backflow_human_request_id = request_id


class FakeClosureService:
    def __init__(self):
        self.resolved = []
        self.request_id = uuid.uuid4()
        self.request = FakeClosureRequest(self.request_id)

    async def get_request_for_orchestration(self, orchestration_id):
        return self.request

    async def get_decision_for_request(self, request_id):
        return None

    async def resolve_review(self, request_id, *, decision, comment=None):
        self.resolved.append((request_id, decision, comment))
        return object()


class FakeIssue:
    def __init__(self, issue_type, severity):
        self.issue_type = issue_type
        self.severity = severity


class FakeCheck:
    def __init__(self, status):
        self.status = status


class FakeAudit:
    def __init__(self, issues):
        self.issues = issues


def _build_service(monkeypatch, *, check_status, issues, sessionmaker=None):
    """构造 ResearchOrchestrationService，patch repo 类 + 注入 fake 守卫服务。"""

    service = ResearchOrchestrationService(
        sessionmaker=sessionmaker or FakeSessionmaker(),
        plan_service=object(),
        stage5_runner=object(),
        orchestration_runner=object(),
        execution_manager=object(),
        source_preparation=None,
        report_audit_service=object(),
        report_check_service=object(),
        closure_service=object(),
    )

    orchestration = FakeOrchestrationRow()

    class FakeRepo:
        def __init__(self, session):
            pass

        async def get_by_id(self, orchestration_id):
            return orchestration

        async def mark_completed(self, orchestration_id, completed_at):
            orchestration.status = "completed"

    class FakeChildRepo:
        def __init__(self, session):
            pass

        async def get_child(self, orchestration_id, stage, attempt_no):
            return type("FakeChild", (), {"workflow_run_id": uuid.uuid4()})()

    monkeypatch.setattr(
        "app.research_orchestration.service.ResearchOrchestrationRepository", FakeRepo
    )
    monkeypatch.setattr(
        "app.research_orchestration.service.ResearchOrchestrationChildRepository",
        FakeChildRepo,
    )

    class FakeOrchestrationRunner:
        async def read_orchestration_checkpoint(self, orchestration_id):
            return {"backflow_round": 2}

    class FakeStage5Runner:
        async def read_checkpoint_state(self, run_id):
            return {"audit_id": uuid.uuid4(), "check_result_id": uuid.uuid4()}

    class FakeCheckService:
        async def verify_check_result_integrity(self, check_result_id):
            return FakeCheck(check_status)

    class FakeAuditService:
        async def verify_audit_integrity(self, audit_id):
            return FakeAudit(issues)

    service._orchestration_runner = FakeOrchestrationRunner()
    service._stage5_runner = FakeStage5Runner()
    service._report_check_service = FakeCheckService()
    service._report_audit_service = FakeAuditService()
    return service


def _bind_closure(service, monkeypatch, *, decision, with_view=False):
    """注入 fake closure service + patch 终态方法（cancel / get_orchestration）。"""
    closure = FakeClosureService()
    service._closure_service = closure

    async def fake_cancel(orchestration_id):
        return "cancelled"

    async def fake_get_orchestration(orchestration_id):
        return "completed"

    monkeypatch.setattr(service, "cancel_orchestration", fake_cancel)
    monkeypatch.setattr(service, "get_orchestration", fake_get_orchestration)
    return closure


# ------------------------------------------------------------------ acceptance guard


@pytest.mark.asyncio
async def test_accept_blocked_when_check_fails(monkeypatch):
    service = _build_service(monkeypatch, check_status="fail", issues=[])
    closure = _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_ACCEPT)
    with pytest.raises(BackflowReviewNotAcceptable) as exc:
        await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_ACCEPT)
    assert any("确定性校验失败" in b for b in exc.value.barriers)
    assert closure.resolved == []


@pytest.mark.asyncio
async def test_accept_blocked_when_critical_issue(monkeypatch):
    service = _build_service(
        monkeypatch,
        check_status="pass",
        issues=[FakeIssue("evidence_mismatch", "critical")],
    )
    _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_ACCEPT)
    with pytest.raises(BackflowReviewNotAcceptable) as exc:
        await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_ACCEPT)
    assert any("critical" in b for b in exc.value.barriers)


@pytest.mark.asyncio
async def test_accept_blocked_when_high_unresolved_conflict(monkeypatch):
    service = _build_service(
        monkeypatch,
        check_status="pass",
        issues=[FakeIssue("unresolved_conflict", "high")],
    )
    _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_ACCEPT)
    with pytest.raises(BackflowReviewNotAcceptable) as exc:
        await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_ACCEPT)
    assert any("数字冲突" in b for b in exc.value.barriers)


@pytest.mark.asyncio
async def test_accept_allowed_for_non_critical_issues(monkeypatch):
    service = _build_service(
        monkeypatch,
        check_status="pass",
        issues=[FakeIssue("wording_overclaim", "low")],
    )
    closure = _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_ACCEPT)
    result = await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_ACCEPT)
    assert result == "completed"
    assert closure.resolved == [(closure.request_id, BACKFLOW_DECISION_ACCEPT, None)]
    # mark_completed 把 orchestration 行置为 completed（FakeRepo 已记录）。


@pytest.mark.asyncio
async def test_extra_research_schedules_bounded_round(monkeypatch):
    from app.research_orchestration.contracts import RESUME_KIND_SUPPLEMENTAL_RESEARCH

    service = _build_service(monkeypatch, check_status="pass", issues=[])
    closure = _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_EXTRA_RESEARCH)
    scheduled = []
    service._execution_manager = type(
        "FakeExecutionManager",
        (),
        {"schedule_resume": lambda self, orch_id, kind: scheduled.append((orch_id, kind))},
    )()
    await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_EXTRA_RESEARCH)
    assert closure.resolved[-1][1] == BACKFLOW_DECISION_EXTRA_RESEARCH
    assert scheduled and scheduled[0][1] == RESUME_KIND_SUPPLEMENTAL_RESEARCH


@pytest.mark.asyncio
async def test_cancel_clean_terminal(monkeypatch):
    service = _build_service(monkeypatch, check_status="pass", issues=[])
    closure = _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_CANCEL)
    result = await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_CANCEL)
    assert result == "cancelled"
    assert closure.resolved[-1][1] == BACKFLOW_DECISION_CANCEL


@pytest.mark.asyncio
async def test_accept_rejected_when_already_finished(monkeypatch):

    service = _build_service(monkeypatch, check_status="pass", issues=[])
    closure = _bind_closure(service, monkeypatch, decision=BACKFLOW_DECISION_ACCEPT)

    class FinishedRepo:
        def __init__(self, session):
            pass

        async def get_by_id(self, orchestration_id):
            return FakeOrchestrationRow(status="completed", phase="completed")

    monkeypatch.setattr(
        "app.research_orchestration.service.ResearchOrchestrationRepository", FinishedRepo
    )
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await service.act_on_backflow_review(uuid.uuid4(), BACKFLOW_DECISION_ACCEPT)
    assert closure.resolved == []
