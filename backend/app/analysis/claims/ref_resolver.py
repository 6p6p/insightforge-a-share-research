"""Evidence ref resolution (stage 4B.1): E<number> → evidence_card_id。

- 模型只能输出 E1..En（Evidence Pack 的局部 alias）；程序 resolve E →
  evidence_card_id，**不 fuzzy resolve、不自动猜 UUID**；
- 未知 E（不在包内 / 格式合法但超出包数量，如只有 E1..E3 却引用 E99）→
  ClaimAnalysisUnknownEvidenceRef；
- 同一 ref 在同一 Claim 内跨 support / contradict / context 重复 →
  ClaimAnalysisRelationConflict（与 ClaimDraft v1 跨 relation 不变量一致）；
- 同一 ref 在同一 relation 组内重复：组内去重（与 ClaimDraft normalization 一致）；
- **所有 candidate 先完成 schema + ref resolution**，任何 candidate 无效 →
  整次分析失败、0 写（避免 partial persistence）。
"""

from dataclasses import dataclass
from uuid import UUID

from app.analysis.claims.contracts import (
    ClaimAnalysisDecision,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
    EvidencePack,
)
from app.analysis.claims.errors import (
    ClaimAnalysisRelationConflict,
    ClaimAnalysisUnknownEvidenceRef,
)


@dataclass(frozen=True)
class ResolvedClaim:
    """解析完成、可直接构造 ClaimDraft 的 Claim 候选（relation → UUID 已 resolve）。"""

    statement: str
    claim_kind: ClaimKind
    confidence: ClaimConfidence
    importance: ClaimImportance
    supports: tuple[UUID, ...]
    contradicts: tuple[UUID, ...]
    context: tuple[UUID, ...]


def resolve_decision_refs(
    decision: ClaimAnalysisDecision,
    pack: EvidencePack,
) -> list[ResolvedClaim]:
    """把 decision 中全部 ref 解析为 evidence_card_id；任一无效 → 抛错（0 写）。"""
    if not decision.claims:
        return []
    resolved: list[ResolvedClaim] = []
    for candidate in decision.claims:
        groups = {
            "supports": candidate.support_refs,
            "contradicts": candidate.contradict_refs,
            "context": candidate.context_refs,
        }
        # 未知引用检查（ref 格式已在 ClaimCandidate schema 校验）。
        for ref in (ref for refs in groups.values() for ref in refs):
            if ref not in pack.ref_to_card_id:
                raise ClaimAnalysisUnknownEvidenceRef(f"unknown evidence ref: {ref}")
        # 跨 relation 重复检查（同一 ref 出现在 ≥2 个 relation 组）。
        relation_by_ref: dict[str, str] = {}
        for relation, refs in groups.items():
            for ref in refs:
                if ref in relation_by_ref:
                    raise ClaimAnalysisRelationConflict(
                        f"evidence ref in multiple relations: {ref}"
                    )
                relation_by_ref[ref] = relation
        # 组内去重 + canonical 排序（与 ClaimDraft normalization 一致）。
        supports = sorted({pack.ref_to_card_id[ref] for ref in groups["supports"]}, key=str)
        contradicts = sorted({pack.ref_to_card_id[ref] for ref in groups["contradicts"]}, key=str)
        context = sorted({pack.ref_to_card_id[ref] for ref in groups["context"]}, key=str)
        resolved.append(
            ResolvedClaim(
                statement=candidate.statement,
                claim_kind=candidate.claim_kind,
                confidence=candidate.confidence,
                importance=candidate.importance,
                supports=tuple(supports),
                contradicts=tuple(contradicts),
                context=tuple(context),
            )
        )
    return resolved
