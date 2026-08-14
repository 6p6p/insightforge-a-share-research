"""Multi-stage no-audit eval variant (stage 7B.1.4C.2).

复用生产多阶段流水线到 Stage5 first draft 为止；**不执行** audit / review
routing / revision / research backflow / human review。v1 只支持 document-only
输入与 document-only 计划（稳定 fail-fast）。
"""

from app.eval.variants.multi_stage_no_audit.contracts import (
    CITATION_KEY_PREFIX,
    MULTI_STAGE_NO_AUDIT_CLAIM_TYPE,
    MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
    MultiStageModelFactoryBundle,
)
from app.eval.variants.multi_stage_no_audit.factory import (
    create_multi_stage_model_factory_bundle,
    create_multi_stage_no_audit_runner,
)
from app.eval.variants.multi_stage_no_audit.runner import MultiStageNoAuditVariantRunner

__all__ = [
    "CITATION_KEY_PREFIX",
    "MULTI_STAGE_NO_AUDIT_CLAIM_TYPE",
    "MULTI_STAGE_NO_AUDIT_PROMPT_VERSION",
    "MultiStageModelFactoryBundle",
    "MultiStageNoAuditVariantRunner",
    "create_multi_stage_model_factory_bundle",
    "create_multi_stage_no_audit_runner",
]
