"""Structured evaluation provenance remap (stage 7B.1.4C.3).

Frozen structured artifact（financial observation / valuation observation /
valuation comparison）在 materialization 时携带 **stable semantic provenance**
（source document content_sha256 + evidence statement/quote + peer company
identity），**不**绑定旧 runtime EvidenceCard UUID。rehydration 时 Evaluation
禁止 seed 历史 EvidenceCard——本 attempt 的 EvidenceCard 由 retrieval →
extraction 重新生成，随后 `StructuredEvidenceRemapService` 按 frozen semantic
provenance 把 structured artifact **重新绑定**到新 EvidenceCard 并确定性重算
fingerprint（attempt-scoped），供 production pipeline（financial /
valuation executors / Stage4）消费。

不变量：
- 不复制 / 不 seed 历史 EvidenceCard（remap 只绑定本 attempt 生成的新卡）；
- 不降低 provenance 校验：frozen payload 的 envelope identity 逐字节校验；
  evidence 解析失败 / 歧义 → `EvalRemapError`（稳定 fail-fast，不静默绕过）；
- financial metrics 仍 deterministically recomputable（frozen source_value_text
  + raw_unit → Decimal → fingerprint 重算只换 evidence card id）；
- valuation inputs 仍 traceable（observation → 新 EvidenceCard → frozen
  content_sha256 → 原 source）。
"""

from app.eval.remap.contracts import (
    RemappedObservation,
    StructuredRemapResult,
)
from app.eval.remap.service import StructuredEvidenceRemapService

__all__ = [
    "RemappedObservation",
    "StructuredRemapResult",
    "StructuredEvidenceRemapService",
]
