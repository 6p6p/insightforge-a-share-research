"""Top-level research orchestration production wiring (7A.2B.2 spec S).

生产装配：`Settings + async_sessionmaker + LangGraphCheckpointManager →
ResearchOrchestrationDependencies / ResearchOrchestrationRunner`。

复用现有 production factories（**构造 0 model call / 0 network / 0 DB 连接**，
所有 model adapter 惰性加载，与 fulfillment / stage4 / stage5 factory 一致）：
- `create_research_fulfillment_service`：plan / router / preparation / fulfillment
  同一批服务实例（spec S：顶层编排节点与 fulfill 共享，保证 plan fingerprint /
  route verify 一致性）；
- `create_stage4_dependencies` + `Stage4WorkflowRunner`：Stage4 exact child；
- `create_stage5_dependencies` + `Stage5WorkflowRunner`：Stage5 exact child +
  execute / resume + checkpoint 投影（7A.2B.2 spec K/L/M）；
- `LangGraphCheckpointManager`：顶层 + child 共用的 PG Checkpointer
  （顶层 `thread_id=orchestration_id`，child `thread_id=run_id`，spec N）。

自动测试不调用本 factory（测试直接构造 deps 并注入 Fake model）；真实调用只
用于生产 / 受控 smoke（0 real DeepSeek 约束对自动测试仍然成立）。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.research_fulfillment.factory import create_research_fulfillment_service
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import ResearchOrchestrationChildService
from app.stage4.dependencies import create_stage4_dependencies
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.dependencies import create_stage5_dependencies
from app.stage5.runner import Stage5WorkflowRunner
from app.synthesis.service import SynthesisService
from app.workflows.checkpoint import LangGraphCheckpointManager


def create_research_orchestration_dependencies(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    checkpoint_manager: LangGraphCheckpointManager,
) -> ResearchOrchestrationDependencies:
    """按 Settings 装配完整顶层编排依赖（0 model call / 0 network）。"""
    fulfillment = create_research_fulfillment_service(settings, sessionmaker)
    stage4_runner = Stage4WorkflowRunner(
        sessionmaker,
        checkpoint_manager,
        create_stage4_dependencies(settings, sessionmaker),
    )
    stage5_runner = Stage5WorkflowRunner(
        sessionmaker,
        checkpoint_manager,
        create_stage5_dependencies(settings, sessionmaker),
    )
    child_service = ResearchOrchestrationChildService(sessionmaker, stage4_runner, stage5_runner)
    return ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=fulfillment.plan_service,
        router=fulfillment.router,
        preparation=fulfillment.preparation,
        fulfillment=fulfillment,
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
    )


def create_research_orchestration_runner(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    checkpoint_manager: LangGraphCheckpointManager,
    dependencies: ResearchOrchestrationDependencies | None = None,
) -> ResearchOrchestrationRunner:
    """按 Settings 装配顶层编排 runner（复用 deps；不传则重建）。"""
    deps = dependencies or create_research_orchestration_dependencies(
        settings, sessionmaker, checkpoint_manager
    )
    return ResearchOrchestrationRunner(sessionmaker, checkpoint_manager, deps)
