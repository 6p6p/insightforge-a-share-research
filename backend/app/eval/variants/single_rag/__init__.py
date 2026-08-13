"""Single RAG evaluation variant (stage 7B.1.4C.1).

第一个真实可执行 baseline：一次语义检索 + 一次 LLM 生成 → `EvalVariantOutput`。
公开符号从此子包直接导入（**不**从 `app.eval.variants` 顶层 re-export，避免
`contracts` ↔ `variants` import 环）。
"""

from app.eval.variants.single_rag.adapter import DeepSeekSingleRagAnswerModel
from app.eval.variants.single_rag.contracts import (
    SINGLE_RAG_PROMPT_VERSION,
    SingleRagAnswerModel,
    SingleRagContextEntry,
    SingleRagModelClaim,
    SingleRagModelOutput,
    build_single_rag_messages,
)
from app.eval.variants.single_rag.factory import create_single_rag_runner
from app.eval.variants.single_rag.runner import SingleRagVariantRunner

__all__ = [
    "SINGLE_RAG_PROMPT_VERSION",
    "DeepSeekSingleRagAnswerModel",
    "SingleRagAnswerModel",
    "SingleRagContextEntry",
    "SingleRagModelClaim",
    "SingleRagModelOutput",
    "build_single_rag_messages",
    "create_single_rag_runner",
    "SingleRagVariantRunner",
]
