"""InsightForge full variant contracts (stage 7B.1.4C.4).

`insightforge_full` 是**真正 production pipeline** 的 evaluation variant：
Frozen bundle → 隔离 runtime → 生产 Planner → Source Router → Fulfillment →
Evidence → Stage4 → Synthesis → Stage5（deterministic checks → **semantic Audit
→ Review Routing → Revision → Research Backflow**，如果真实 workflow 判断需要）
→ final Report → `EvalVariantOutput`。

`FullModelFactoryBundle` 是模型身份 + per-attempt 模型构造契约：runner 在任何
factory call 前校验 `bundle.provider / bundle.model_id == EvalExecutionConfig.model`
（不一致 → `EvalExecutionAssemblyError`，0 factory call），随后在每次 run 内用这组
factory 创建本 attempt 的生产模型（绑定 config-bound settings + per-attempt
usage_observer）。**runner 不 import 任何生产 adapter**：production factory 创建
真实 DeepSeek adapters，E2E / 单测注入 fake factory bundle 即可。

Human Review evaluation policy（不跳过 Audit / Revision）：
- production graph 的 `wait_human` interrupt 保留（Audit → Review Routing 真实
  执行）；当路由到 human_review 时，evaluation 用确定性 policy 自动裁决
  `approve`（`finalize_on_approve` 仍强制 deterministic Check=pass，spec R——
  人工裁决不能覆盖 Gate 0）；该 policy 可复现、不改变 Full 核心能力；
- 若 orchestration 停在 `waiting_manual`（frozen snapshot 无法满足计划）→
  稳定 fail-fast（frozen 输入不完整，不伪造 readiness）。
"""

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.contracts import ClaimAnalysisModel
from app.analysis.financial.contracts import FinancialAnalysisModel
from app.analysis.macro.model import MacroAnalysisModel
from app.analysis.synthesis.model import SynthesisAnalysisModel
from app.analysis.valuation.model import ValuationAnalysisModel
from app.audit.model import AuditModel
from app.draft_section.model import DraftSectionModel
from app.evidence.extractor.contracts import EvidenceExtractionModel
from app.llm.instrumentation import LlmUsageObserver
from app.research_planning.planner import ResearchPlannerModel
from app.revision.model import RevisionWriterModel
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage5.dependencies import Stage5WorkflowDependencies

INSIGHTFORGE_FULL_PROMPT_VERSION = "v1"

# normalized `EvalClaim.claim_type`：本 variant 全部 claim 来自生产 Stage4
# claim analysis（确定性归一化，不取模型原始 claim_kind）。
INSIGHTFORGE_FULL_CLAIM_TYPE = "claim"

# 归一化 citation 的稳定短 key 前缀（E1 / E2 / E3 ...）。
CITATION_KEY_PREFIX = "E"

# evaluation human-review policy 的自动裁决（approve；Check=pass 由生产
# finalize_on_approve 强制）。
EVAL_HUMAN_DECISION = "approve"

# human-review 自动裁决的最大轮次（防御性上限；正常流程 <=1）。
MAX_EVAL_HUMAN_ROUNDS = 3


@dataclass(frozen=True)
class FullModelFactoryBundle:
    """insightforge_full 的模型身份 + per-attempt 模型构造契约（装配期注入）。

    - `provider` / `model_id`：must equal `EvalExecutionConfig.model`（runner 在
      **任何** factory call 前校验，不一致 → assembly error，0 factory call）；
    - 10 个 `create_*` callable 在 **run 时**被 runner 调用（每次传入本 attempt 的
      `usage_observer`），各自返回真实生产 adapter 或测试 fake——runner 不 import
      任何 DeepSeek adapter；
    - `create_stage4_deps` / `create_stage5_deps`：返回完整生产 deps 结构（内部
      复用本 bundle 的 model factories），使 Stage4 / Stage5 graph 的全部模型构造
      同样可注入、observer 线程一致。
    """

    provider: str
    model_id: str
    create_planner: Callable[[LlmUsageObserver | None], ResearchPlannerModel]
    create_evidence: Callable[[LlmUsageObserver | None], EvidenceExtractionModel]
    create_claim: Callable[[LlmUsageObserver | None], ClaimAnalysisModel]
    create_financial: Callable[[LlmUsageObserver | None], FinancialAnalysisModel]
    create_macro: Callable[[LlmUsageObserver | None], MacroAnalysisModel]
    create_valuation: Callable[[LlmUsageObserver | None], ValuationAnalysisModel]
    create_synthesis: Callable[[LlmUsageObserver | None], SynthesisAnalysisModel]
    create_draft: Callable[[LlmUsageObserver | None], DraftSectionModel]
    create_audit: Callable[[LlmUsageObserver | None], AuditModel]
    create_revision: Callable[[LlmUsageObserver | None], RevisionWriterModel]
    create_stage4_deps: Callable[
        [async_sessionmaker, LlmUsageObserver | None], Stage4AnalysisDependencies
    ]
    create_stage5_deps: Callable[
        [async_sessionmaker, LlmUsageObserver | None], Stage5WorkflowDependencies
    ]
