"""Evaluation variant identity (stage 7B.1.0).

三路系统评估的 variant：single_rag / multi_stage_no_audit / insightforge_full。
`noop` / `test` / `mock` **不属于** `EvalVariantId`；未来 dev/test runner 用独立
identity，不进入 comparable 集合。

本目录同时承载各 variant 实现（`single_rag/` 子包），但 `EvalVariantId` /
`COMPARABLE_VARIANTS` 必须可被 `app.eval.contracts` 等模块**无环**导入：因此本
`__init__` **不** re-export `single_rag`（后者 import `app.eval.contracts`，而
`contracts` import 本包，会造成 import 环）。`single_rag` 的公开符号从
`app.eval.variants.single_rag` 直接导入。
"""

from enum import StrEnum


class EvalVariantId(StrEnum):
    SINGLE_RAG = "single_rag"
    MULTI_STAGE_NO_AUDIT = "multi_stage_no_audit"
    INSIGHTFORGE_FULL = "insightforge_full"


# 参与三路比较的冻结集合（顺序稳定；不含 noop / test / mock）。
COMPARABLE_VARIANTS: tuple[EvalVariantId, ...] = tuple(EvalVariantId)


__all__ = ["EvalVariantId", "COMPARABLE_VARIANTS"]
