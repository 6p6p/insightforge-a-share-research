"""Evidence ref resolution unit tests (stage 4B.1)。

校验 E<number> → evidence_card_id 的确定性解析：
- 正常解析：ref → UUID，按 relation 分组；
- 未知 E（超出包数量 / 不在包内）→ ClaimAnalysisUnknownEvidenceRef，不 fuzzy；
- 跨 relation 重复（同一 ref 在 supports+context 等）→ ClaimAnalysisRelationConflict；
- 组内去重 + canonical 排序（与 ClaimDraft normalization 一致）；
- relevant=false（无 claims）→ []。

**零真实 LLM / 零 DB**。
"""

from uuid import UUID

import pytest

from app.analysis.claims.contracts import (
    ClaimAnalysisDecision,
    ClaimCandidate,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.analysis.claims.errors import (
    ClaimAnalysisRelationConflict,
    ClaimAnalysisUnknownEvidenceRef,
)
from app.analysis.claims.evidence_pack import EvidencePackSource, build_evidence_pack
from app.analysis.claims.ref_resolver import resolve_decision_refs

_E_A = UUID("aaaaaaaa-1111-1111-1111-111111111111")
_E_B = UUID("bbbbbbbb-2222-2222-2222-222222222222")
_E_C = UUID("cccccccc-3333-3333-3333-333333333333")


def _source(card_id: UUID) -> EvidencePackSource:
    return EvidencePackSource(
        evidence_card_id=card_id,
        evidence_statement="陈述",
        evidence_type="metric",
        origin_type="document_chunk",
        authority_tier_snapshot=3,
        provider_key="xinhuanet",
    )


def _pack(card_ids: list[UUID]):
    return build_evidence_pack([_source(card_id) for card_id in card_ids])


def _candidate(**overrides) -> ClaimCandidate:
    values = dict(
        statement="海外业务是公司收入增长的重要驱动因素",
        claim_kind=ClaimKind.INFERENCE,
        confidence=ClaimConfidence.MEDIUM,
        importance=ClaimImportance.NORMAL,
        support_refs=["E1"],
        contradict_refs=[],
        context_refs=[],
    )
    values.update(overrides)
    return ClaimCandidate(**values)


def test_resolves_refs_to_uuids_per_relation() -> None:
    pack = _pack([_E_A, _E_B])
    decision = ClaimAnalysisDecision(
        relevant=True,
        claims=[
            _candidate(support_refs=["E1"], contradict_refs=["E2"]),
            _candidate(statement="另一条观点", support_refs=["E2"], context_refs=["E1"]),
        ],
    )
    resolved = resolve_decision_refs(decision, pack)
    assert len(resolved) == 2
    assert resolved[0].supports == (_E_A,)
    assert resolved[0].contradicts == (_E_B,)
    assert resolved[1].supports == (_E_B,)
    assert resolved[1].context == (_E_A,)


def test_unknown_ref_rejected_without_fuzzy_resolve() -> None:
    pack = _pack([_E_A, _E_B])  # 只有 E1..E2
    decision = ClaimAnalysisDecision(relevant=True, claims=[_candidate(support_refs=["E99"])])
    with pytest.raises(ClaimAnalysisUnknownEvidenceRef):
        resolve_decision_refs(decision, pack)


def test_unknown_ref_not_in_pack_but_in_range_rejected() -> None:
    pack = _pack([_E_A, _E_B])
    decision = ClaimAnalysisDecision(relevant=True, claims=[_candidate(context_refs=["E2", "E3"])])
    # E3 格式合法但包内不存在（只有 E1..E2）→ 仍拒绝，不自动猜。
    with pytest.raises(ClaimAnalysisUnknownEvidenceRef):
        resolve_decision_refs(decision, pack)


def test_cross_relation_duplicate_rejected() -> None:
    pack = _pack([_E_A, _E_B])
    decision = ClaimAnalysisDecision(
        relevant=True,
        claims=[_candidate(support_refs=["E1"], context_refs=["E1"])],
    )
    with pytest.raises(ClaimAnalysisRelationConflict):
        resolve_decision_refs(decision, pack)


def test_within_relation_refs_sorted_canonically() -> None:
    # 组内重复已在 ClaimCandidate schema 拒绝；resolver 对已去重输入做 canonical
    # 排序（与 ClaimDraft normalization 一致）。
    pack = _pack([_E_B, _E_A])
    decision = ClaimAnalysisDecision(relevant=True, claims=[_candidate(support_refs=["E2", "E1"])])
    resolved = resolve_decision_refs(decision, pack)
    # E1 < E2 按 str(uuid) 排序。
    assert resolved[0].supports == (_E_A, _E_B)


def test_non_relevant_decision_returns_empty() -> None:
    pack = _pack([_E_A])
    decision = ClaimAnalysisDecision(relevant=False, claims=[])
    assert resolve_decision_refs(decision, pack) == []
