"""Startup recovery wiring order（7A.2B.2 spec T）。

正式 startup 顺序保证：1. `WorkflowRecoveryService` reconcile orphaned WorkflowRuns
→ 2. `ResearchOrchestrationRecoveryCoordinator` → 3. legacy
`ResearchExecutionRecoveryCoordinator`（只处理 non-owned runs）。

本文件 mock 三个恢复步骤记录调用顺序，只验证顺序保证；不触碰 DB / 真实 graph
（0 real DeepSeek / 0 network 约束保持）。
"""

from fastapi.testclient import TestClient

from app.research_orchestration.recovery import ResearchOrchestrationRecoveryCoordinator
from app.services.research_execution_recovery import ResearchExecutionRecoveryCoordinator
from app.services.workflow_recovery_service import WorkflowRecoveryService


def test_lifespan_recovery_order(app, monkeypatch) -> None:
    """reconcile → orchestration coordinator → legacy coordinator，绝不交叉。"""
    order: list[str] = []

    async def _reconcile(self) -> None:
        order.append("reconcile")

    async def _orch(self) -> int:
        order.append("orchestration")
        return 0

    async def _legacy(self) -> int:
        order.append("legacy")
        return 0

    monkeypatch.setattr(WorkflowRecoveryService, "reconcile_orphaned_runs", _reconcile)
    monkeypatch.setattr(ResearchOrchestrationRecoveryCoordinator, "recover_orchestrations", _orch)
    monkeypatch.setattr(ResearchExecutionRecoveryCoordinator, "recover_interrupted_chains", _legacy)

    with TestClient(app) as _:
        pass

    assert order == ["reconcile", "orchestration", "legacy"]


def test_lifespan_recovery_unbound_runner_skips_orchestration(app, monkeypatch) -> None:
    """orchestration_runner 未绑定（异常配置）→ 跳过 orchestration recovery，
    不阻塞 legacy 链（失败不阻止启动）。"""
    order: list[str] = []

    async def _reconcile(self) -> None:
        order.append("reconcile")

    async def _legacy(self) -> int:
        order.append("legacy")
        return 0

    monkeypatch.setattr(WorkflowRecoveryService, "reconcile_orphaned_runs", _reconcile)
    monkeypatch.setattr(ResearchExecutionRecoveryCoordinator, "recover_interrupted_chains", _legacy)
    # lifespan 构造的 orchestration service 总是绑定 runner；模拟未绑定场景：
    # 让 _create_research_orchestration 返回的 service 的 property 返回 None。
    from app.research_orchestration.service import ResearchOrchestrationService

    monkeypatch.setattr(
        ResearchOrchestrationService,
        "orchestration_runner",
        property(lambda self: None),
    )

    with TestClient(app) as _:
        pass

    assert order == ["reconcile", "legacy"]
