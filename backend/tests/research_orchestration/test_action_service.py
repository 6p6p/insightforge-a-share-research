"""Research orchestration `act_on_orchestration` unit tests（7A.2B.2 spec N/O/P，0 DB）。

人工裁决生命周期：
- **N**：`act_on_orchestration` **仅 waiting_human**——非 waiting_human / 非
  awaiting_stage5 / 未知 action → 拒绝（400 / 409）；cancel 单独委托
  `cancel_orchestration`（含自身幂等规则）；
- **O**：`approve` → `resume_stage5_human(child_run, decision=approve, comment)`
  提交 immutable human decision，再 `run_orchestration` 继续顶层（continuation →
  complete）。rewrite / research 走同一 dispatch；
- **P**：`research` → child resume(decision=research) → 顶层 continuation →
  pause_for_research 只持久化 research_request_id + phase=research_backflow
  （本单测验证 dispatch 转发；终态持久化由 graph 层 / 集成 Cases 覆盖）；
- runners 未绑定（approve/rewrite/research）→ RuntimeError（programming error）。

真实 continuation 语义由 test_graph_execution.py（编译后 graph）证明。
"""

from types import SimpleNamespace
from uuid import UUID

import pytest

from app.core.errors import WorkflowRunAlreadyFinished
from app.research_orchestration.contracts import (
    ChildStage,
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.errors import (
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationApprovalRejected,
    ResearchOrchestrationChildNotFound,
    ResearchOrchestrationInvalidAction,
    ResearchOrchestrationNotFound,
)
from app.research_orchestration.repository import (
    ResearchOrchestrationChildRepository,
    ResearchOrchestrationRepository,
)
from app.research_orchestration.service import ResearchOrchestrationService
from app.review.contracts import (
    HUMAN_DECISION_APPROVE,
    HUMAN_DECISION_CANCEL,
    HUMAN_DECISION_RESEARCH,
    HUMAN_DECISION_REWRITE,
)
from app.stage5.errors import Stage5ApproveRequiresPassCheck
from tests.research_orchestration.fakes import (
    FakeActionOrchestrationRunner,
    FakeActionStage5Runner,
    FakePlanService,
    FakeSessionMaker,
    make_orchestration,
)

pytestmark = pytest.mark.asyncio

_OID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
_STAGE5_RUN_ID = UUID("00000000-0000-0000-0000-000000000009")


def _waiting_stage5_row() -> SimpleNamespace:
    return make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status=OrchestrationStatus.WAITING_HUMAN.value,
        current_phase=OrchestrationPhase.AWAITING_STAGE5.value,
    )


def _stage5_child():
    return SimpleNamespace(workflow_run_id=_STAGE5_RUN_ID)


def _service(monkeypatch, *, bind_runners: bool = True) -> ResearchOrchestrationService:
    return ResearchOrchestrationService(
        FakeSessionMaker(),
        FakePlanService(),
        stage5_runner=FakeActionStage5Runner() if bind_runners else None,
        orchestration_runner=FakeActionOrchestrationRunner() if bind_runners else None,
    )


def _bind_row(monkeypatch, row) -> None:
    async def fake_get(self, orchestration_id):
        return row

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        assert stage == ChildStage.STAGE5.value
        assert attempt_no == 1
        return _stage5_child()

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)


# ------------------------------------------------------------------ dispatch


async def test_act_approve_resumes_child_and_continues(monkeypatch) -> None:
    """approve：resume_stage5_human(child, decision=approve, comment=None) →
    run_orchestration(_OID) → 返回 fresh orchestration 行（spec O）。"""
    service = _service(monkeypatch)
    _bind_row(monkeypatch, _waiting_stage5_row())

    result = await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)

    assert service._stage5_runner.resumes == [(_STAGE5_RUN_ID, HUMAN_DECISION_APPROVE, None)]
    assert service._orchestration_runner.run_calls == [_OID]
    assert result.orchestration_id == _OID
    assert result.status == OrchestrationStatus.WAITING_HUMAN.value


async def test_act_rewrite_passes_comment(monkeypatch) -> None:
    """rewrite：comment 原样透传到 resume_stage5_human（immutable decision）。"""
    service = _service(monkeypatch)
    _bind_row(monkeypatch, _waiting_stage5_row())

    await service.act_on_orchestration(_OID, HUMAN_DECISION_REWRITE, comment="补充收入分拆")

    assert service._stage5_runner.resumes == [
        (_STAGE5_RUN_ID, HUMAN_DECISION_REWRITE, "补充收入分拆")
    ]
    assert service._orchestration_runner.run_calls == [_OID]


async def test_act_research_dispatches_research_decision(monkeypatch) -> None:
    """research：child resume(decision=research) → 顶层 continuation（spec P）。"""
    service = _service(monkeypatch)
    _bind_row(monkeypatch, _waiting_stage5_row())

    await service.act_on_orchestration(_OID, HUMAN_DECISION_RESEARCH)

    assert service._stage5_runner.resumes == [(_STAGE5_RUN_ID, HUMAN_DECISION_RESEARCH, None)]
    assert service._orchestration_runner.run_calls == [_OID]


async def test_act_cancel_delegates_to_cancel(monkeypatch) -> None:
    """cancel：委托 cancel_orchestration（本用例证明 dispatch，不重复测 cancel 规则）。"""
    service = _service(monkeypatch)
    delegated: list = []

    async def fake_cancel(self, orchestration_id):
        delegated.append(orchestration_id)
        return make_orchestration(status=OrchestrationStatus.CANCELLED.value)

    monkeypatch.setattr(ResearchOrchestrationService, "cancel_orchestration", fake_cancel)

    result = await service.act_on_orchestration(_OID, HUMAN_DECISION_CANCEL)

    assert delegated == [_OID]
    assert result.status == OrchestrationStatus.CANCELLED.value
    # cancel 不触碰 stage5 / orchestration runners。
    assert service._stage5_runner.resumes == []
    assert service._orchestration_runner.run_calls == []


# ------------------------------------------------------------------ guards


async def test_act_unknown_action_rejected(monkeypatch) -> None:
    service = _service(monkeypatch)
    _bind_row(monkeypatch, _waiting_stage5_row())

    with pytest.raises(ResearchOrchestrationInvalidAction):
        await service.act_on_orchestration(_OID, "banana")


async def test_act_rejects_terminal_orchestration(monkeypatch) -> None:
    """status=completed → AlreadyFinished（仅 waiting_human 可裁决）。"""
    service = _service(monkeypatch)
    row = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status=OrchestrationStatus.COMPLETED.value,
        current_phase=OrchestrationPhase.COMPLETED.value,
    )
    _bind_row(monkeypatch, row)

    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)


async def test_act_rejects_non_awaiting_phase(monkeypatch) -> None:
    """status=waiting_human 但 phase=research_backflow → InvalidAction（无 Stage5
    child 待裁决，research 已提出）。"""
    service = _service(monkeypatch)
    row = make_orchestration(
        orchestration_id=_OID,
        task_id=_TASK_ID,
        status=OrchestrationStatus.WAITING_HUMAN.value,
        current_phase=OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    _bind_row(monkeypatch, row)

    with pytest.raises(ResearchOrchestrationInvalidAction):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)


async def test_act_missing_orchestration(monkeypatch) -> None:
    service = _service(monkeypatch)

    async def fake_get(self, orchestration_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)

    with pytest.raises(ResearchOrchestrationNotFound):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)


async def test_act_missing_stage5_child(monkeypatch) -> None:
    """exact stage5 child 不存在（无法裁决）→ ChildNotFound。"""
    service = _service(monkeypatch)

    async def fake_get(self, orchestration_id):
        return _waiting_stage5_row()

    async def fake_get_child(self, orchestration_id, stage, attempt_no):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationChildRepository, "get_child", fake_get_child)

    with pytest.raises(ResearchOrchestrationChildNotFound):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)


async def test_act_unbound_runners_raise(monkeypatch) -> None:
    """runners 未绑定（approve/rewrite/research）→ RuntimeError，不猜归属。"""
    service = _service(monkeypatch, bind_runners=False)
    _bind_row(monkeypatch, _waiting_stage5_row())

    with pytest.raises(RuntimeError, match="action runners not bound"):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)


async def test_act_approve_requires_pass_check_projections_failed(monkeypatch) -> None:
    """v1.2.4 polish：approve 被确定性阻断（Stage5ApproveRequiresPassCheck，
    REPORT_BLOCKING 真实性/证据问题）→ orchestration 同步投影 failed
    （error_code=stage5_approval_rejected）+ 抛 409 ApprovalRejected；
    **顶层 run_orchestration 不再执行**（child 已 FAILED，二次点击直接
    AlreadyFinished，不重复阻塞式拒绝）。"""
    service = _service(monkeypatch)
    _bind_row(monkeypatch, _waiting_stage5_row())

    async def fake_resume(self, run_id, decision, comment=None):
        raise Stage5ApproveRequiresPassCheck()

    async def fake_mark_failed(self, orchestration_id, completed_at, *, error_code, error_message=None):
        assert error_code == "stage5_approval_rejected"
        assert error_message and "阻断" in error_message
        return make_orchestration(
            orchestration_id=_OID,
            task_id=_TASK_ID,
            status=OrchestrationStatus.FAILED.value,
            current_phase=OrchestrationPhase.STAGE5.value,
            error_code=error_code,
        )

    monkeypatch.setattr(FakeActionStage5Runner, "resume_stage5_human", fake_resume)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", fake_mark_failed)

    with pytest.raises(ResearchOrchestrationApprovalRejected):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)

    # 顶层 run 未被调用：approve 被拒即终态投影，不继续顶层（防二次执行）。
    assert service._orchestration_runner.run_calls == []


async def test_act_approve_child_already_finished_also_projections_failed(
    monkeypatch,
) -> None:
    """v1.2.4 polish：僵尸态「第二次点击」——child run 已终态（前次拒绝已标
    FAILED / 外部已结束），resume 抛 WorkflowRunAlreadyFinished → 同样投影
    orchestration failed + 抛 409（不再「工作流已结束」后仍卡 waiting_human）。"""
    service = _service(monkeypatch)
    _bind_row(monkeypatch, _waiting_stage5_row())

    async def fake_resume(self, run_id, decision, comment=None):
        raise WorkflowRunAlreadyFinished()

    async def fake_mark_failed(self, orchestration_id, completed_at, *, error_code, error_message=None):
        assert error_code == "stage5_approval_rejected"
        return make_orchestration(
            orchestration_id=_OID,
            task_id=_TASK_ID,
            status=OrchestrationStatus.FAILED.value,
            current_phase=OrchestrationPhase.STAGE5.value,
            error_code=error_code,
        )

    monkeypatch.setattr(FakeActionStage5Runner, "resume_stage5_human", fake_resume)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", fake_mark_failed)

    with pytest.raises(ResearchOrchestrationApprovalRejected):
        await service.act_on_orchestration(_OID, HUMAN_DECISION_APPROVE)

    assert service._orchestration_runner.run_calls == []
