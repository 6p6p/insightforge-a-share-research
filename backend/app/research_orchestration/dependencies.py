"""Top-level research orchestration dependencies (stage 7A.2B.1 spec J + 7A.2B.2 L).

顶层 graph 的节点**复用**现有 services，不复制业务逻辑：
`ResearchPlanningService`（ensure_plan）→ `ResearchSourceRouter`（ensure_route）
→ `ResearchPreparationService`（prepare）→ `ResearchFulfillmentService`
（fulfill）→ `ResearchOrchestrationChildService` + `Stage4WorkflowRunner`
（Stage4 exact child）→ `SynthesisService`（collect_synthesis verify）→
`ResearchOrchestrationChildService` + `Stage5WorkflowRunner`（Stage5 exact child +
execute/resume + checkpoint 投影，7A.2B.2 spec K/L/M）。

Stage4/5 保持独立 WorkflowRun（`thread_id=run_id`），顶层用自己的 checkpointer
（`thread_id=orchestration_id`）——两个线程互不冲突（spec N）。
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_orchestration.service import ResearchOrchestrationChildService
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.synthesis.service import SynthesisService


@dataclass(frozen=True)
class ResearchOrchestrationDependencies:
    """顶层 graph 节点依赖（集中 DI；测试注入 Fake 实现）。"""

    sessionmaker: async_sessionmaker
    plan_service: ResearchPlanningService
    router: ResearchSourceRouter
    preparation: ResearchPreparationService
    fulfillment: ResearchFulfillmentService
    child_service: ResearchOrchestrationChildService
    stage4_runner: Stage4WorkflowRunner
    synthesis_service: SynthesisService
    stage5_runner: Stage5WorkflowRunner
