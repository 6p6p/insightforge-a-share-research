"""InsightForge full eval variant (stage 7B.1.4C.4).

真正 production pipeline 的 evaluation variant：Frozen input → 隔离 runtime →
生产 Planner → Source Router → Fulfillment → Evidence → Stage4 → Synthesis →
Stage5（checks → **Audit → Review Routing → Revision → Research Backflow**）→
final Report → `EvalVariantOutput`。Human Review 用确定性 evaluation policy
（自动 approve；Check=pass 由生产节点强制）。
"""

from app.eval.variants.insightforge_full.contracts import (
    CITATION_KEY_PREFIX,
    EVAL_HUMAN_DECISION,
    INSIGHTFORGE_FULL_CLAIM_TYPE,
    INSIGHTFORGE_FULL_PROMPT_VERSION,
    MAX_EVAL_HUMAN_ROUNDS,
    FullModelFactoryBundle,
)
from app.eval.variants.insightforge_full.factory import (
    create_full_model_factory_bundle,
    create_insightforge_full_runner,
)
from app.eval.variants.insightforge_full.runner import InsightForgeFullVariantRunner

__all__ = [
    "CITATION_KEY_PREFIX",
    "EVAL_HUMAN_DECISION",
    "INSIGHTFORGE_FULL_CLAIM_TYPE",
    "INSIGHTFORGE_FULL_PROMPT_VERSION",
    "MAX_EVAL_HUMAN_ROUNDS",
    "FullModelFactoryBundle",
    "InsightForgeFullVariantRunner",
    "create_full_model_factory_bundle",
    "create_insightforge_full_runner",
]
