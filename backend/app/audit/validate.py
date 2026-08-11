"""Hard coverage + ref validation + resolution (stage 5D, spec M/N).

确定性代码在 LLM 调用结束后、持久化之前，对 `AuditDecision` 执行：

- **No-cherry-picking coverage（spec M）**：`reviewed_paragraph_refs` 必须**恰好
  等于** pack P refs。遗漏 → `ReportAuditParagraphOmitted`；重复 →
  `ReportAuditParagraphDuplicate`；unknown → `ReportAuditUnknownRef`。
- **Known / scope ref validation（spec N）**：
  - `section_ref` 必须真实存在（S<number> 在 pack sections 内）；
  - `paragraph_ref` 若存在必须属于该 section（`paragraph.section_ref` 精确匹配）；
  - `claim_refs` 必须属于该 paragraph referenced Claims（v1 更严格：只允许该
    paragraph 引用的 C，不允许 direct-related context / 跨 section）；
  - `evidence_refs` 必须属于该 issue `claim_refs` 绑定的 Audit Evidence
    （`evidence.claim_ids` 与 issue claim_ids 有交集）——禁止给 C1 挂只属于 C7
    的 E9。
- **Section-level issue**（`paragraph_ref` = None）只允许空 claim_refs /
  evidence_refs（没有 paragraph 就没有 claim scope，v1 严格拒绝 fuzzy）。

`validate_decision` 验证通过后把 alias / index 解析回真实 section_id /
paragraph_index / claim_id / evidence_card_id，产出**按 canonical key 排序**的
`ResolvedAuditIssue` 序列（persisted review_issues 与 audit_fingerprint 的
规范化数据源——排序保证指纹对输出顺序鲁棒）。

任何 hard boundary 违反 → 对应 `ReportAuditError`（**0 partial write**，不自动
repair）。message 只描述审核问题，不写新公司事实。
"""

from uuid import UUID

from app.audit.contracts import AuditDecision, ResolvedAuditIssue
from app.audit.errors import (
    ReportAuditParagraphDuplicate,
    ReportAuditParagraphOmitted,
    ReportAuditUnknownRef,
)
from app.audit.packs import AuditPack, AuditPackEvidence


def validate_decision(pack: AuditPack, decision: AuditDecision) -> list[ResolvedAuditIssue]:
    """hard coverage + ref validation，并解析 alias / index 回真实 ID。

    返回按 canonical key 排序的 `ResolvedAuditIssue`（0..50 条，persisted
    review_issues 与 audit_fingerprint 的规范化数据源）。
    """
    _check_coverage(pack, decision.reviewed_paragraph_refs)

    claim_by_ref = {item.claim_ref: item for item in pack.claims}
    evidence_by_ref = {item.evidence_ref: item for item in pack.evidence}

    resolved = [
        _resolve_issue(pack, issue, claim_by_ref, evidence_by_ref) for issue in decision.issues
    ]
    resolved.sort(key=_canonical_key)
    return resolved


def _check_coverage(pack: AuditPack, reviewed_paragraph_refs: list[str]) -> None:
    """spec M：reviewed_paragraph_refs 恰好等于 pack 全部 P refs。"""
    if len(reviewed_paragraph_refs) != len(set(reviewed_paragraph_refs)):
        raise ReportAuditParagraphDuplicate("reviewed_paragraph_refs 不得重复")

    pack_refs = {paragraph.paragraph_ref for paragraph in pack.paragraphs}
    reviewed_set = set(reviewed_paragraph_refs)
    if reviewed_set == pack_refs:
        return

    missing = sorted(pack_refs - reviewed_set)
    if missing:
        raise ReportAuditParagraphOmitted("reviewed_paragraph_refs 遗漏段落: " + ", ".join(missing))
    unknown = sorted(reviewed_set - pack_refs)
    raise ReportAuditUnknownRef("reviewed_paragraph_refs 引用未知段落: " + ", ".join(unknown))


def _section_or_raise(pack: AuditPack, section_ref: str) -> "object":
    try:
        return pack.section_by_ref(section_ref)
    except StopIteration as exc:
        raise ReportAuditUnknownRef(f"issue.section_ref {section_ref} 不存在") from exc


def _paragraph_or_raise(pack: AuditPack, paragraph_ref: str) -> "object":
    try:
        return pack.paragraph_by_ref(paragraph_ref)
    except StopIteration as exc:
        raise ReportAuditUnknownRef(f"issue.paragraph_ref {paragraph_ref} 不存在") from exc


def _resolve_issue(
    pack: AuditPack,
    issue,
    claim_by_ref: dict,
    evidence_by_ref: dict,
) -> ResolvedAuditIssue:
    section = _section_or_raise(pack, issue.section_ref)

    paragraph = None
    paragraph_index = None
    if issue.paragraph_ref is not None:
        paragraph = _paragraph_or_raise(pack, issue.paragraph_ref)
        if paragraph.section_ref != issue.section_ref:
            raise ReportAuditUnknownRef(
                f"issue.paragraph_ref {issue.paragraph_ref} 不属于 section {issue.section_ref}"
            )
        paragraph_index = paragraph.paragraph_index

    if paragraph is None and issue.claim_refs:
        raise ReportAuditUnknownRef("section-level issue（paragraph_ref=None）不得携带 claim_refs")

    # v1 严格（spec N）：claim_refs 只允许该 paragraph referenced Claims。
    allowed_claim_refs = set(paragraph.claim_refs) if paragraph is not None else set()
    claim_ids: list[UUID] = []
    for ref in issue.claim_refs:
        if ref not in claim_by_ref:
            raise ReportAuditUnknownRef(f"issue 引用未知 claim alias {ref}")
        if ref not in allowed_claim_refs:
            raise ReportAuditUnknownRef(
                f"issue 的 claim {ref} 不在该 paragraph referenced claims 内"
            )
        claim_ids.append(claim_by_ref[ref].claim_id)

    # evidence_refs 必须绑定到 issue claim_refs（spec N：禁止 cross-scope）。
    claim_id_set = {str(claim_id) for claim_id in claim_ids}
    evidence_card_ids: list[UUID] = []
    for ref in issue.evidence_refs:
        item = _evidence_or_raise(evidence_by_ref, ref)
        bound_claim_ids = {str(claim_id) for claim_id, _ in item.claim_relations}
        if not (bound_claim_ids & claim_id_set):
            raise ReportAuditUnknownRef(
                f"issue 的 evidence {ref} 未绑定到该 issue 的任一 claim_refs"
            )
        evidence_card_ids.append(item.evidence_card_id)

    return ResolvedAuditIssue(
        issue_type=issue.issue_type,
        severity=issue.severity,
        section_id=section.section_id,
        paragraph_index=paragraph_index,
        message=issue.message,
        related_claim_ids=tuple(sorted(str(cid) for cid in set(claim_ids))),
        related_evidence_card_ids=tuple(sorted(str(cid) for cid in set(evidence_card_ids))),
    )


def _evidence_or_raise(evidence_by_ref: dict, ref: str) -> AuditPackEvidence:
    try:
        return evidence_by_ref[ref]
    except KeyError as exc:
        raise ReportAuditUnknownRef(f"issue 引用未知 evidence alias {ref}") from exc


def _canonical_key(item: ResolvedAuditIssue) -> tuple:
    """canonical 排序 key（audit_fingerprint 对输出顺序鲁棒）。"""
    return (
        item.section_id,
        -1 if item.paragraph_index is None else item.paragraph_index,
        item.issue_type,
        item.severity,
        item.message,
        item.related_claim_ids,
        item.related_evidence_card_ids,
    )
