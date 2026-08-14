"""Multi-stage no-audit variant contracts (stage 7B.1.4C.2).

`multi_stage_no_audit` 复用生产多阶段流水线，但**明确不执行** audit /
review routing / revision / research backflow / human review：

    Frozen bundle → isolated replay → parse/chunk/index
        → ResearchPlanningService → ResearchSourceRouter
        → ResearchFulfillmentService → EvidenceExtractionService
        → Stage4 Claim Analysis → Synthesis → Stage5 first draft
        → STOP → EvalVariantOutput

v1 只支持 document-only 输入与 document-only 计划（稳定 fail-fast）；planner /
evidence / claim / synthesis / draft 全部绑定 frozen `EvalExecutionConfig.model`
（provider / model_id 均无硬编码）。

`MultiStageModelFactoryBundle` 是**模型身份 + per-attempt 模型构造**契约：runner
在**任何** factory call 前校验 `bundle.provider / bundle.model_id ==
EvalExecutionConfig.model`（不一致 → `EvalExecutionAssemblyError`，0 factory
call），随后在每次 run 内用这组 factory 创建本 attempt 的 5 个生产模型（绑定
config-bound settings + per-attempt usage_observer）。**runner 不 import 任何
生产 adapter**：production factory 创建真实 DeepSeek adapters，E2E / 单测注入
fake factory 即可；`create_stage4_deps` 复用 create_claim / create_synthesis
（fakes 可传播进 Stage4），另以生产 adapter 提供 document-only 下从不 dispatch
的 financial / macro / valuation analysis services。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.contracts import ClaimAnalysisModel
from app.analysis.synthesis.model import SynthesisAnalysisModel
from app.draft_section.model import DraftSectionModel
from app.evidence.extractor.contracts import EvidenceExtractionModel
from app.llm.instrumentation import LlmUsageObserver
from app.research_planning.planner import ResearchPlannerModel
from app.stage4.dependencies import Stage4AnalysisDependencies

MULTI_STAGE_NO_AUDIT_PROMPT_VERSION = "v1"

# normalized `EvalClaim.claim_type`：本 variant 全部 claim 来自生产 Stage4
# claim analysis（确定性归一化，不取模型原始 claim_kind）。
MULTI_STAGE_NO_AUDIT_CLAIM_TYPE = "claim"

# 归一化 citation 的稳定短 key 前缀（E1 / E2 / E3 ...）。
CITATION_KEY_PREFIX = "E"


@dataclass(frozen=True)
class MultiStageModelFactoryBundle:
    """multi_stage_no_audit 的模型身份 + per-attempt 模型构造契约（装配期注入）。

    - `provider` / `model_id`：must equal `EvalExecutionConfig.model`（runner 在
      **任何** factory call 前校验，不一致 → assembly error，0 factory call）；
    - 5 个 `create_*` callable 在 **run 时**被 runner 调用（每次传入本 attempt 的
      `usage_observer`），各自返回真实生产 adapter 或测试 fake——runner 不 import
      任何 DeepSeek adapter，杜绝「校验 Model A、实际调用 Model B」；
    - `create_stage4_deps(sessionmaker, observer)`：返回完整
      `Stage4AnalysisDependencies`（内部复用本 bundle 的 create_claim /
      create_synthesis + 生产 financial/macro/valuation adapters），使 Stage4
      graph 的全部模型构造同样可注入、observer 线程一致。
    """

    provider: str
    model_id: str
    create_planner: Callable[[LlmUsageObserver | None], ResearchPlannerModel]
    create_evidence: Callable[[LlmUsageObserver | None], EvidenceExtractionModel]
    create_claim: Callable[[LlmUsageObserver | None], ClaimAnalysisModel]
    create_synthesis: Callable[[LlmUsageObserver | None], SynthesisAnalysisModel]
    create_draft: Callable[[LlmUsageObserver | None], DraftSectionModel]
    create_stage4_deps: Callable[
        [async_sessionmaker, LlmUsageObserver | None], Stage4AnalysisDependencies
    ]
