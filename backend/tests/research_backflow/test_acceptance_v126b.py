"""v1.2.6-B（任务1）：报告已生成即允许人工接受——系统级 barrier 保留。

覆盖 acceptance guard 的两个新分支：
- report_id 存在但 audit/check 缺失（degraded）-> SECTION_WARNING 允许接受；
- report_id 缺失（工作流状态损坏）-> REPORT_BLOCKING barrier（系统级阻断保留）。
"""

from __future__ import annotations

import uuid

import pytest

from app.audit.severity import AuditImpactScope
from app.research_orchestration.service import ResearchOrchestrationService
from tests.research_backflow.test_closure_unit import (
    FakeOrchestrationRow,
    FakeSessionmaker,
)


class _FakeRepo:
    def __init__(self, session):
        self.row = FakeOrchestrationRow()

    async def get_by_id(self, orchestration_id):
        return self.row


class _FakeChildRepo:
    def __init__(self, session):
        pass

    async def get_child(self, orchestration_id, stage, attempt_no):
        return type("FakeChild", (), {"workflow_run_id": uuid.uuid4()})()


class _FakeOrchestrationRunner:
    async def read_orchestration_checkpoint(self, orchestration_id):
        return {"backflow_round": 2}


def _make_service(monkeypatch, *, checkpoint) -> ResearchOrchestrationService:
    service = ResearchOrchestrationService(
        sessionmaker=FakeSessionmaker(),
        plan_service=object(),
    )
    monkeypatch.setattr(
        "app.research_orchestration.service.ResearchOrchestrationRepository", _FakeRepo
    )
    monkeypatch.setattr(
        "app.research_orchestration.service.ResearchOrchestrationChildRepository",
        _FakeChildRepo,
    )
    service._orchestration_runner = _FakeOrchestrationRunner()

    class _FakeS5:
        async def read_checkpoint_state(self, run_id):
            return checkpoint

    service._stage5_runner = _FakeS5()
    # 守卫服务缺省 None（acceptance guard 中 audit/check 服务未绑定不影响新分支判定）
    return service


@pytest.mark.asyncio
async def test_accept_allowed_when_report_exists_but_audit_check_missing(monkeypatch):
    # report_id 存在（报告已生成）+ audit/check 记录缺失（degraded / 审核失败）
    # -> 不再产生「当前报告暂不能接受」barrier；放行允许人工接受（SECTION_WARNING）。
    service = _make_service(
        monkeypatch,
        checkpoint={"report_id": str(uuid.uuid4())},
    )
    scope, barriers = await service._acceptance_evaluation(uuid.uuid4())
    assert scope == AuditImpactScope.SECTION_WARNING
    assert barriers == []


@pytest.mark.asyncio
async def test_accept_blocked_when_no_report_at_all(monkeypatch):
    # 无任何报告（工作流状态损坏 / 无法定位报告）-> 系统级 barrier 保留，拒绝接受。
    service = _make_service(monkeypatch, checkpoint={})
    scope, barriers = await service._acceptance_evaluation(uuid.uuid4())
    assert scope == AuditImpactScope.REPORT_BLOCKING
    assert len(barriers) == 1
    assert "无法定位当前报告" in barriers[0]
