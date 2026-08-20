"""Audit pack construction (stage 5D, spec J/K): deterministic aliases + verified input.

角色边界（Auditor 只判断"语义上是否真的成立"）：
- 代码确定性构造 S1..Sn / P1..Pm / C1..Cn / E1..En / X1..Xn / G1..Gn alias：
  Section 按 section_order、Paragraph 按 section_order + paragraph_index、Claim
  按 str(claim_id)、Evidence 按 str(evidence_card_id) canonical 排序，conflict /
  gap 按 outline index 顺序——**LLM 永不看 UUID / fingerprint / provenance id**；
- pack 只投影最小字段（spec J）；服务层经 pack 的 alias map 解析回真实
  claim_id / evidence_card_id / paragraph_index（未知编号 → 拒绝）；
- Evidence 不只是 paragraph 已引用的：对 paragraph referenced Claims，加载这些
  Claim 当前绑定的**全部** ClaimEvidenceLinks（supports / contradicts / context），
  让 Auditor 能看到"作者只引用了 supports E1，但 Claim 其实还有 contradicts E2"。

身份（spec Q）：pack 携带 claim_fingerprint / evidence_fingerprint / claim_relations /
conflict-gap 数据，供 `audit_pack_identity` 产出 canonical 指纹身份——**不渲染进
prompt**。
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.report.contracts import VerifiedReport, VerifiedReportCheckResult


@dataclass(frozen=True)
class LoadedAuditClaim:
    """短 DB session 加载的 audit Claim（含 fingerprint，供输入指纹用）。"""

    claim_id: UUID
    claim_fingerprint: str
    statement: str
    analysis_domain: str
    claim_kind: str
    confidence: str
    importance: str


@dataclass(frozen=True)
class LoadedAuditEvidence:
    """短 DB session 加载的 audit Evidence（含 fingerprint；按 card 聚合）。

    - claim_relations：`(claim_id, relation)` 元组序列——每张 Evidence 与其绑定
      Claims 的**per-Claim relation**（canonical 排序，supports / contradicts /
      context 全部保留，不折叠）；
    - `claim_ids`（property）：绑定 Claims 的 id 集（由 claim_relations 派生）。
    """

    evidence_card_id: UUID
    evidence_fingerprint: str
    evidence_statement: str
    evidence_type: str
    quote_text: str | None
    provider_key: str
    authority_tier: int
    critical_eligible: bool
    source_published_at: datetime | None
    reporting_period_end: date | None
    origin_type: str
    claim_relations: tuple[tuple[UUID, str], ...]

    @property
    def claim_ids(self) -> tuple[UUID, ...]:
        return tuple(claim_id for claim_id, _ in self.claim_relations)


@dataclass(frozen=True)
class ResolvedAuditConflict:
    """synthesis 冲突（claim_refs 已解析回真实 claim_id；按 outline index）。"""

    claim_ids: tuple[UUID, ...]
    description: str
    severity: str
    resolution_direction: str


@dataclass(frozen=True)
class ResolvedAuditGap:
    """synthesis 证据缺口（claim_refs 已解析回真实 claim_id；按 outline index）。"""

    claim_ids: tuple[UUID, ...]
    description: str
    suggested_evidence: str | None
    priority: str


@dataclass(frozen=True)
class AuditPackSection:
    """一条 Report section 的投影（S alias + 身份，不含正文）。"""

    section_ref: str
    section_id: str
    section_type: str
    title: str


@dataclass(frozen=True)
class AuditPackParagraph:
    """一个 Report 段落的投影（P alias + 最小字段 + deterministic finding codes）。

    - claim_refs / evidence_refs：该段落实际引用的 C / E alias；
    - check_finding_codes：该段落命中的 deterministic Check finding codes
      （section_id + paragraph_index 精确匹配），只做机械提示，不是语义判断；
    - claim_ids / evidence_card_ids 只供服务层 ref resolution（alias map），
      **永不渲染进 prompt**。
    """

    paragraph_ref: str
    section_ref: str
    paragraph_index: int
    section_id: str
    section_title: str
    text: str
    claim_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    check_finding_codes: tuple[str, ...]
    claim_ids: tuple[UUID, ...]
    evidence_card_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class AuditPackClaim:
    """一条 Report Claim 的投影（C alias + 最小字段，不含 UUID / fingerprint）。"""

    claim_ref: str
    claim_id: UUID
    claim_fingerprint: str
    analysis_domain: str
    claim_kind: str
    statement: str
    confidence: str
    importance: str


@dataclass(frozen=True)
class AuditPackEvidence:
    """一条 Evidence 的投影（E alias + 最小字段，不含 UUID / fingerprint / locator）。

    - claim_relations：`(claim_id, relation)` 元组序列——Auditor 能看到
      contradicts / context 等未在段落中引用的绑定关系（spec J）；
    - claim_aliases（property）：绑定 Claims 的 audit C alias（由 claim_relations
      派生，prompt 渲染用）。
    """

    evidence_ref: str
    evidence_card_id: UUID
    evidence_fingerprint: str
    claim_relations: tuple[tuple[UUID, str], ...]
    evidence_type: str
    evidence_statement: str
    quote_text: str | None
    provider_key: str
    authority_tier: int
    critical_eligible: bool
    period: str | None
    published: str | None
    origin_type: str

    @property
    def claim_aliases(self) -> tuple[str, ...]:
        return tuple(sorted((claim_id for claim_id, _ in self.claim_relations), key=str))


@dataclass(frozen=True)
class AuditPackConflict:
    """一条 synthesis 冲突的投影（X alias + 描述 / 严重度 / 解决方向）。

    claim_aliases 只投影**本 pack** 的 C alias（过滤掉不在 Report 的 Claim）。
    """

    conflict_ref: str
    claim_ids: tuple[UUID, ...]
    claim_aliases: tuple[str, ...]
    description: str
    severity: str
    resolution_direction: str


@dataclass(frozen=True)
class AuditPackGap:
    """一个 synthesis 证据缺口的投影（G alias + 描述 / 建议证据 / 优先级）。"""

    gap_ref: str
    claim_ids: tuple[UUID, ...]
    claim_aliases: tuple[str, ...]
    description: str
    suggested_evidence: str | None
    priority: str


@dataclass(frozen=True)
class AuditPack:
    """传给 Auditor 模型的一次性输入（Report 全量段落 + Claims + 全部绑定 Evidence
    + deterministic findings + synthesis conflicts/gaps）。

    claim_id / evidence_card_id / fingerprint 只供服务层 ref resolution 与输入
    指纹，**永不渲染进 prompt**；LLM 可见投影不含 UUID / fingerprint / locator /
    URL / RawArtifact / Chroma metadata。
    """

    sections: tuple[AuditPackSection, ...]
    paragraphs: tuple[AuditPackParagraph, ...]
    claims: tuple[AuditPackClaim, ...]
    evidence: tuple[AuditPackEvidence, ...]
    conflicts: tuple[AuditPackConflict, ...]
    gaps: tuple[AuditPackGap, ...]

    def section_by_ref(self, ref: str) -> AuditPackSection:
        return next(item for item in self.sections if item.section_ref == ref)

    def section_by_id(self, section_id: str) -> AuditPackSection:
        return next(item for item in self.sections if item.section_id == section_id)

    def paragraph_by_ref(self, ref: str) -> AuditPackParagraph:
        return next(item for item in self.paragraphs if item.paragraph_ref == ref)

    def paragraphs_for_section(self, section_id: str) -> tuple[AuditPackParagraph, ...]:
        return tuple(item for item in self.paragraphs if item.section_id == section_id)

    def claim_by_ref(self, ref: str) -> AuditPackClaim:
        return next(item for item in self.claims if item.claim_ref == ref)

    def claim_by_id(self, claim_id: UUID) -> AuditPackClaim:
        return next(item for item in self.claims if item.claim_id == claim_id)

    def evidence_by_ref(self, ref: str) -> AuditPackEvidence:
        return next(item for item in self.evidence if item.evidence_ref == ref)

    def evidence_by_id(self, evidence_card_id: UUID) -> AuditPackEvidence:
        return next(item for item in self.evidence if item.evidence_card_id == evidence_card_id)

    def paragraph_count(self) -> int:
        return len(self.paragraphs)


def build_audit_pack(
    *,
    verified_report: VerifiedReport,
    verified_check: VerifiedReportCheckResult,
    claims: list[LoadedAuditClaim],
    evidence: list[LoadedAuditEvidence],
    conflicts: list[ResolvedAuditConflict],
    gaps: list[ResolvedAuditGap],
) -> AuditPack:
    """纯函数：把 verified Report + verified CheckResult + 加载产物构造成 Audit Pack。

    - S 按 section_order、P 按 section_order + paragraph_index；
    - C 按 str(claim_id)、E 按 str(evidence_card_id) canonical 排序；
    - X / G 按 outline index 顺序（conflicts / gaps 列表顺序）；
    - Evidence 的 claim_aliases / Conflict / Gap 的 claim_aliases 只投影**本 pack**
      的 C alias（过滤掉不在 Report 的 id）。
    """
    sections = tuple(
        AuditPackSection(
            section_ref=f"S{index}",
            section_id=section.section_id,
            section_type=section.section_type,
            title=section.title,
        )
        for index, section in enumerate(verified_report.verified_outline.sections, start=1)
    )

    ordered_claims = sorted(claims, key=lambda claim: str(claim.claim_id))
    claim_items = tuple(
        AuditPackClaim(
            claim_ref=f"C{index}",
            claim_id=claim.claim_id,
            claim_fingerprint=claim.claim_fingerprint,
            analysis_domain=claim.analysis_domain,
            claim_kind=claim.claim_kind,
            statement=claim.statement,
            confidence=claim.confidence,
            importance=claim.importance,
        )
        for index, claim in enumerate(ordered_claims, start=1)
    )
    claim_id_to_ref = {str(item.claim_id): item.claim_ref for item in claim_items}

    ordered_evidence = sorted(evidence, key=lambda item: str(item.evidence_card_id))
    evidence_items = tuple(
        AuditPackEvidence(
            evidence_ref=f"E{index}",
            evidence_card_id=item.evidence_card_id,
            evidence_fingerprint=item.evidence_fingerprint,
            claim_relations=tuple(sorted(item.claim_relations, key=lambda pair: str(pair[0]))),
            evidence_type=item.evidence_type,
            evidence_statement=item.evidence_statement,
            quote_text=item.quote_text,
            provider_key=item.provider_key,
            authority_tier=item.authority_tier,
            critical_eligible=item.critical_eligible,
            period=item.reporting_period_end.isoformat() if item.reporting_period_end else None,
            published=item.source_published_at.isoformat() if item.source_published_at else None,
            origin_type=item.origin_type,
        )
        for index, item in enumerate(ordered_evidence, start=1)
    )
    evidence_id_to_ref = {str(item.evidence_card_id): item.evidence_ref for item in evidence_items}

    finding_by_paragraph: dict[tuple[str, int], list[str]] = {}
    for finding in verified_check.findings:
        if finding.section_id is not None and finding.paragraph_index is not None:
            finding_by_paragraph.setdefault(
                (finding.section_id, finding.paragraph_index), []
            ).append(finding.code)

    paragraphs: list[AuditPackParagraph] = []
    for section in sections:
        for payload_section in verified_report.report_payload["sections"]:
            if payload_section.get("section_id") == section.section_id:
                for index, paragraph in enumerate(payload_section["paragraphs"]):
                    claim_ids = tuple(UUID(raw) for raw in paragraph.get("claim_ids", []) or [])
                    evidence_card_ids = tuple(
                        UUID(raw) for raw in paragraph.get("evidence_card_ids", []) or []
                    )
                    paragraphs.append(
                        AuditPackParagraph(
                            paragraph_ref=f"P{len(paragraphs) + 1}",
                            section_ref=section.section_ref,
                            paragraph_index=index,
                            section_id=section.section_id,
                            section_title=section.title,
                            text=paragraph["text"],
                            claim_refs=tuple(
                                sorted(claim_id_to_ref[str(cid)] for cid in claim_ids)
                            ),
                            evidence_refs=tuple(
                                sorted(
                                    evidence_id_to_ref[str(cid)]
                                    for cid in evidence_card_ids
                                    if str(cid) in evidence_id_to_ref
                                )
                            ),
                            check_finding_codes=tuple(
                                sorted(finding_by_paragraph.get((section.section_id, index), []))
                            ),
                            claim_ids=claim_ids,
                            evidence_card_ids=evidence_card_ids,
                        )
                    )

    def _claim_aliases(claim_ids) -> tuple[str, ...]:
        return tuple(
            sorted(claim_id_to_ref[str(cid)] for cid in claim_ids if str(cid) in claim_id_to_ref)
        )

    conflict_items = tuple(
        AuditPackConflict(
            conflict_ref=f"X{index}",
            claim_ids=conflict.claim_ids,
            claim_aliases=_claim_aliases(conflict.claim_ids),
            description=conflict.description,
            severity=conflict.severity,
            resolution_direction=conflict.resolution_direction,
        )
        for index, conflict in enumerate(conflicts, start=1)
    )
    gap_items = tuple(
        AuditPackGap(
            gap_ref=f"G{index}",
            claim_ids=gap.claim_ids,
            claim_aliases=_claim_aliases(gap.claim_ids),
            description=gap.description,
            suggested_evidence=gap.suggested_evidence,
            priority=gap.priority,
        )
        for index, gap in enumerate(gaps, start=1)
    )

    return AuditPack(
        sections=sections,
        paragraphs=tuple(paragraphs),
        claims=claim_items,
        evidence=evidence_items,
        conflicts=conflict_items,
        gaps=gap_items,
    )


def audit_pack_identity(pack: AuditPack) -> dict:
    """normalized audit pack identity（spec Q，供 `audit_input_fingerprint`）。

    只投影**指纹与结构身份**，不投影 LLM 可见正文——正文变化会改变
    report_fingerprint / claim_fingerprint / evidence_fingerprint → 新输入指纹，
    无需重复携带 text：
    - sections：section_ref / section_id / section_type / title（结构）；
    - paragraphs：paragraph_ref / section_ref / paragraph_index / section_id /
      claim_refs / evidence_refs / check_finding_codes（段落结构 + 引用映射 +
      deterministic finding codes；**不含正文 text**）；
    - claims：claim_ref / claim_fingerprint（指纹已含 statement 等）；
    - evidence：evidence_ref / evidence_fingerprint / claim_relations
      （按 str(claim_id) 排序的 (claim_id, relation) 映射——ClaimEvidence 关系）；
    - conflicts / gaps：ref / claim_ids / description / severity / priority /
      resolution_direction（synthesis 冲突与缺口身份）。
    """
    return {
        "sections": [
            {
                "section_ref": section.section_ref,
                "section_id": section.section_id,
                "section_type": section.section_type,
                "title": section.title,
            }
            for section in pack.sections
        ],
        "paragraphs": [
            {
                "paragraph_ref": paragraph.paragraph_ref,
                "section_ref": paragraph.section_ref,
                "paragraph_index": paragraph.paragraph_index,
                "section_id": paragraph.section_id,
                "claim_refs": sorted(paragraph.claim_refs),
                "evidence_refs": sorted(paragraph.evidence_refs),
                "check_finding_codes": sorted(paragraph.check_finding_codes),
            }
            for paragraph in pack.paragraphs
        ],
        "claims": [
            {
                "claim_ref": claim.claim_ref,
                "claim_fingerprint": claim.claim_fingerprint,
            }
            for claim in pack.claims
        ],
        "evidence": [
            {
                "evidence_ref": item.evidence_ref,
                "evidence_fingerprint": item.evidence_fingerprint,
                "claim_relations": [
                    [str(claim_id), relation] for claim_id, relation in item.claim_relations
                ],
            }
            for item in pack.evidence
        ],
        "conflicts": [
            {
                "conflict_ref": conflict.conflict_ref,
                "claim_ids": sorted(str(cid) for cid in conflict.claim_ids),
                "description": conflict.description,
                "severity": conflict.severity,
                "resolution_direction": conflict.resolution_direction,
            }
            for conflict in pack.conflicts
        ],
        "gaps": [
            {
                "gap_ref": gap.gap_ref,
                "claim_ids": sorted(str(cid) for cid in gap.claim_ids),
                "description": gap.description,
                "suggested_evidence": gap.suggested_evidence,
                "priority": gap.priority,
            }
            for gap in pack.gaps
        ],
    }
