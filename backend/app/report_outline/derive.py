"""Deterministic outline derivation (stage 5A).

纯函数：`VerifiedSynthesisResult` → v1 outline payload（`{"sections": [...]}`），
**不调用 LLM / 不访问 DB**。确定性映射规则：

- 每个 theme → 一个 theme section（按 persisted normalized order，即输出
  themes 的原顺序）；`title` 用 theme label，**不重写**；
- theme section 的 `claim_ids` = 该 theme 所有**非 duplicate canonical
  Claims**，按 canonical（C alias 顺序）sort + dedupe；
- 存在 conflicts 或 evidence_gaps → 末尾追加一个 risks_and_gaps section
  （固定标题 `OUTLINE_RISKS_AND_GAPS_TITLE`），只存 indexes
  （conflict_indexes / evidence_gap_indexes），**不生成解释正文**；
- coverage 硬边界：所有 input Claim 必须属于某 theme section 的 claim_ids 或
  明确是某个 duplicate 组的非 canonical 成员，否则
  `ReportOutlineClaimCoverageError`（不静默丢 claim / 不猜主题）。
"""

from uuid import UUID

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.report_outline.contracts import (
    OUTLINE_RISKS_AND_GAPS_TITLE,
    SECTION_TYPE_RISKS_AND_GAPS,
    SECTION_TYPE_THEME,
)
from app.report_outline.errors import ReportOutlineClaimCoverageError


def _alias_index(ref: str) -> int:
    """C alias → 序号（C1 < C2 < ... < C10，数值序）。"""
    return int(ref[1:])


def _noncanonical_duplicate_refs(verified: VerifiedSynthesisResult) -> set[str]:
    """全部 duplicate 组的非 canonical 成员（这些 Claim 是重复声明，被 canonical 吸收）。

    canonical_ref 留在主题内（它代表整组重复声明）；非 canonical 成员不单独
    出现在任何 theme section，但 coverage 通过"明确 duplicate_ref"豁免。
    """
    refs: set[str] = set()
    for duplicate in verified.output.duplicates:
        for ref in duplicate.claim_refs:
            if ref != duplicate.canonical_ref:
                refs.add(ref)
    return refs


def derive_outline_payload(verified: VerifiedSynthesisResult) -> dict:
    """纯函数：verified synthesis result → v1 outline payload（sections 列表）。

    返回 canonical dict（可直接 JSONB / fingerprint 化）。不读 DB、不调模型。
    """
    alias_map = verified.alias_map
    duplicate_refs = _noncanonical_duplicate_refs(verified)

    sections: list[dict] = []

    for theme in verified.output.themes:
        order = len(sections) + 1
        # 该 theme 的非 duplicate canonical Claims：按 C alias 顺序 sort + dedupe。
        canonical_refs = sorted(
            {ref for ref in theme.claim_refs if ref not in duplicate_refs},
            key=_alias_index,
        )
        sections.append(
            {
                "section_id": f"S{order}",
                "section_type": SECTION_TYPE_THEME,
                "title": theme.title,
                "claim_ids": [str(alias_map[ref]) for ref in canonical_refs],
                "conflict_indexes": [],
                "evidence_gap_indexes": [],
                "section_order": order,
            }
        )

    if verified.output.conflicts or verified.output.evidence_gaps:
        order = len(sections) + 1
        sections.append(
            {
                "section_id": f"S{order}",
                "section_type": SECTION_TYPE_RISKS_AND_GAPS,
                "title": OUTLINE_RISKS_AND_GAPS_TITLE,
                "claim_ids": [],
                "conflict_indexes": list(range(len(verified.output.conflicts))),
                "evidence_gap_indexes": list(range(len(verified.output.evidence_gaps))),
                "section_order": order,
            }
        )

    # coverage 硬边界：每个 input Claim 必须被某 theme section 覆盖，或明确是
    # duplicate_ref（被 canonical 吸收）。
    covered_claim_ids: set[UUID] = set()
    for section in sections:
        covered_claim_ids.update(UUID(claim_id) for claim_id in section["claim_ids"])
    covered_claim_ids.update(alias_map[ref] for ref in duplicate_refs)
    uncovered = [
        claim_id for claim_id in verified.input_claim_ids if claim_id not in covered_claim_ids
    ]
    if uncovered:
        raise ReportOutlineClaimCoverageError(
            f"report outline does not cover {len(uncovered)} input claim(s)"
        )

    return {"sections": sections}
