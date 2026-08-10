"""Section input pack construction (stage 5B): deterministic aliases + verified input.

角色边界（Writer 只做证据约束的正文起草）：
- 代码确定性构造 C1..Cn / E1..En / X1..Xn / G1..Gn alias：Claim 按
  str(claim_id)、Evidence 按 str(evidence_card_id) canonical 排序，conflict /
  gap 按 outline index 顺序——**LLM 永不看 UUID / fingerprint / provenance id**；
- pack 只投影最小字段（spec G/H/I）；服务层经 pack 的 alias map 解析回真实
  claim_id / evidence_card_id / index（未知编号 → 拒绝）；
- `Loaded*` / `Resolved*` 是服务层短 DB session 的加载产物（纯数据，不可变）。

字段投影说明：
- Claim：claim_ref / domain / kind / statement / confidence / importance；
- Evidence：evidence_ref / claim_refs（绑定 Claim 的 section alias）/ relation /
  evidence_type / evidence_statement / quote_text / provider_key /
  authority_tier / period / published / origin_type；**不含** locator /
  RawArtifact / storage key；
- Conflict / Gap：alias + claim_refs + 描述 / 严重度（或优先级）/ 解决方向
  （或建议证据）。
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.report_outline.contracts import VerifiedReportOutline


@dataclass(frozen=True)
class LoadedClaim:
    """短 DB session 加载的 section Claim（含 fingerprint，供输入指纹用）。"""

    claim_id: UUID
    claim_fingerprint: str
    statement: str
    analysis_domain: str
    claim_kind: str
    confidence: str
    importance: str


@dataclass(frozen=True)
class LoadedEvidence:
    """短 DB session 加载的 section Evidence（含 fingerprint；按 card 聚合）。

    - claim_ids：通过 claim_evidence_links 真实绑定到本 section Claim 的 id 集
      （canonical 排序）；
    - relation：多 (claim, evidence) 关系时取确定性最强关系
      （supports > contradicts > context）。
    """

    evidence_card_id: UUID
    evidence_fingerprint: str
    evidence_statement: str
    evidence_type: str
    quote_text: str | None
    provider_key: str
    authority_tier: int
    reporting_period_end: date | None
    source_published_at: datetime | None
    origin_type: str
    relation: str
    claim_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class ResolvedConflict:
    """risks_and_gaps 恢复的一条冲突（claim_refs 已解析回真实 claim_id）。"""

    claim_ids: tuple[UUID, ...]
    description: str
    severity: str
    resolution_direction: str


@dataclass(frozen=True)
class ResolvedGap:
    """risks_and_gaps 恢复的一个证据缺口（claim_refs 已解析回真实 claim_id）。"""

    claim_ids: tuple[UUID, ...]
    description: str
    suggested_evidence: str | None
    priority: str


@dataclass(frozen=True)
class SectionClaimItem:
    """一条 section Claim 的投影（alias + 最小字段，不含 UUID / fingerprint）。"""

    alias: str
    claim_id: UUID
    analysis_domain: str
    claim_kind: str
    confidence: str
    importance: str
    statement: str


@dataclass(frozen=True)
class SectionEvidenceItem:
    """一条 section Evidence 的投影（alias + 最小字段，不含 provenance id）。"""

    alias: str
    evidence_card_id: UUID
    claim_aliases: tuple[str, ...]
    relation: str
    evidence_type: str
    evidence_statement: str
    quote_text: str | None
    provider_key: str
    authority_tier: int
    period: str | None
    published: str | None
    origin_type: str


@dataclass(frozen=True)
class SectionConflictItem:
    """一条冲突的投影（X alias + 描述 / 严重度 / 解决方向）。"""

    alias: str
    claim_aliases: tuple[str, ...]
    description: str
    severity: str
    resolution_direction: str


@dataclass(frozen=True)
class SectionGapItem:
    """一个证据缺口的投影（G alias + 描述 / 建议证据 / 优先级）。"""

    alias: str
    claim_aliases: tuple[str, ...]
    description: str
    suggested_evidence: str | None
    priority: str


@dataclass(frozen=True)
class SectionInputPack:
    """传给 Writer 模型的一次性输入（research question + cutoff + C/E/X/G packs）。

    claim_id / evidence_card_id 只供服务层 ref resolution（alias map），
    **永不渲染进 prompt**；LLM 可见投影不含 UUID / fingerprint / provenance id。
    """

    company_name: str
    research_question: str
    analysis_as_of: date
    section_id: str
    section_order: int
    section_type: str
    title: str
    claims: tuple[SectionClaimItem, ...]
    evidence: tuple[SectionEvidenceItem, ...]
    conflicts: tuple[SectionConflictItem, ...]
    gaps: tuple[SectionGapItem, ...]

    def claim_alias_map(self) -> dict[str, UUID]:
        """C alias → 真实 claim_id（仅服务层 ref resolution 用，永不进 prompt）。"""
        return {item.alias: item.claim_id for item in self.claims}

    def evidence_alias_map(self) -> dict[str, UUID]:
        """E alias → 真实 evidence_card_id（仅服务层 ref resolution 用）。"""
        return {item.alias: item.evidence_card_id for item in self.evidence}

    def claim_by_alias(self, alias: str) -> SectionClaimItem:
        return next(item for item in self.claims if item.alias == alias)

    def evidence_by_alias(self, alias: str) -> SectionEvidenceItem:
        return next(item for item in self.evidence if item.alias == alias)


def build_section_input_pack(
    *,
    outline: VerifiedReportOutline,
    section,
    company_name: str,
    claims: list[LoadedClaim],
    evidence: list[LoadedEvidence],
    conflicts: list[ResolvedConflict],
    gaps: list[ResolvedGap],
) -> SectionInputPack:
    """纯函数：把加载产物构造成 deterministic Section Input Pack。

    C alias 按 str(claim_id)、E alias 按 str(evidence_card_id) canonical 排序；
    X / G alias 按 outline index 顺序（conflicts / gaps 列表顺序）。Evidence 的
    claim_aliases / Conflict / Gap 的 claim_aliases 只投影**本 section** 的 alias
    （过滤掉绑定到其他 section Claim 的 id——LLM 只能引用本 section 允许的 C）。
    """
    ordered_claims = sorted(claims, key=lambda claim: str(claim.claim_id))
    claim_items = tuple(
        SectionClaimItem(
            alias=f"C{index}",
            claim_id=claim.claim_id,
            analysis_domain=claim.analysis_domain,
            claim_kind=claim.claim_kind,
            confidence=claim.confidence,
            importance=claim.importance,
            statement=claim.statement,
        )
        for index, claim in enumerate(ordered_claims, start=1)
    )
    id_to_alias = {str(item.claim_id): item.alias for item in claim_items}

    def _section_aliases(claim_ids) -> tuple[str, ...]:
        return tuple(sorted(id_to_alias[str(cid)] for cid in claim_ids if str(cid) in id_to_alias))

    ordered_evidence = sorted(evidence, key=lambda item: str(item.evidence_card_id))
    evidence_items = tuple(
        SectionEvidenceItem(
            alias=f"E{index}",
            evidence_card_id=item.evidence_card_id,
            claim_aliases=_section_aliases(item.claim_ids),
            relation=item.relation,
            evidence_type=item.evidence_type,
            evidence_statement=item.evidence_statement,
            quote_text=item.quote_text,
            provider_key=item.provider_key,
            authority_tier=item.authority_tier,
            period=item.reporting_period_end.isoformat() if item.reporting_period_end else None,
            published=item.source_published_at.isoformat() if item.source_published_at else None,
            origin_type=item.origin_type,
        )
        for index, item in enumerate(ordered_evidence, start=1)
    )
    conflict_items = tuple(
        SectionConflictItem(
            alias=f"X{index}",
            claim_aliases=_section_aliases(conflict.claim_ids),
            description=conflict.description,
            severity=conflict.severity,
            resolution_direction=conflict.resolution_direction,
        )
        for index, conflict in enumerate(conflicts, start=1)
    )
    gap_items = tuple(
        SectionGapItem(
            alias=f"G{index}",
            claim_aliases=_section_aliases(gap.claim_ids),
            description=gap.description,
            suggested_evidence=gap.suggested_evidence,
            priority=gap.priority,
        )
        for index, gap in enumerate(gaps, start=1)
    )
    return SectionInputPack(
        company_name=company_name,
        research_question=outline.verified_synthesis_result.research_question,
        analysis_as_of=outline.analysis_as_of,
        section_id=section.section_id,
        section_order=section.section_order,
        section_type=section.section_type,
        title=section.title,
        claims=claim_items,
        evidence=evidence_items,
        conflicts=conflict_items,
        gaps=gap_items,
    )
