"""Evaluation variant identity (stage 7B.1.0).

三路系统评估的 variant：single_rag / multi_stage_no_audit / insightforge_full。
`noop` / `test` / `mock` **不属于** `EvalVariantId`；未来 dev/test runner 用独立
identity，不进入 comparable 集合。
"""

from enum import StrEnum


class EvalVariantId(StrEnum):
    SINGLE_RAG = "single_rag"
    MULTI_STAGE_NO_AUDIT = "multi_stage_no_audit"
    INSIGHTFORGE_FULL = "insightforge_full"


# 参与三路比较的冻结集合（顺序稳定；不含 noop / test / mock）。
COMPARABLE_VARIANTS: tuple[EvalVariantId, ...] = tuple(EvalVariantId)
