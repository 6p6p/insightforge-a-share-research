"""Deterministic claim pack construction (stage 4D.1B).

综合分析只做判断：Claim Pack 由代码确定性构造——C alias（C1..Cn）按
`analysis_domain` + `claim_id` canonical 排序分配，**LLM 永不看 UUID / 永不看
claim_fingerprint**。LLM 输出里的 C 编号经本 pack 的 alias 映射解析回真实
claim_id（服务层 ref resolution 用，未知编号 → 拒绝）。
"""

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.synthesis.contracts import VerifiedSynthesisClaim


@dataclass(frozen=True)
class ClaimPackItem:
    """一条输入 Claim 的投影。

    `claim_id` 只供服务层 ref resolution（`SynthesisClaimPack.alias_map`），
    **永不渲染进 prompt**；LLM 可见投影不含 UUID / fingerprint / provenance id。
    """

    alias: str
    claim_id: UUID
    analysis_domain: str
    claim_kind: str
    confidence: str
    importance: str
    statement: str
    evidence_count: int
    domain_analysis_as_of: date | None


@dataclass(frozen=True)
class SynthesisClaimPack:
    """传给综合分析模型的一次性输入（research question + cutoff + 全部 C alias）。"""

    research_question: str
    analysis_as_of: date
    company_name: str
    items: tuple[ClaimPackItem, ...]

    def alias_map(self) -> dict[str, UUID]:
        """C alias → 真实 claim_id（仅服务层 ref resolution 用，永不进 prompt）。"""
        return {item.alias: item.claim_id for item in self.items}


def build_claim_pack(
    *,
    research_question: str,
    analysis_as_of: date,
    company_name: str,
    claims: list[VerifiedSynthesisClaim],
) -> SynthesisClaimPack:
    """纯函数：把 verified claims 构造成 deterministic Claim Pack。

    C alias 确定性排序：**analysis_domain + str(claim_id)**（spec 固定顺序）。
    调用方负责传入已按 claim_id 去重的 claims；这里只分配编号并投影字段。
    """
    ordered = sorted(claims, key=lambda claim: (claim.analysis_domain.value, str(claim.claim_id)))
    items = tuple(
        ClaimPackItem(
            alias=f"C{index}",
            claim_id=claim.claim_id,
            analysis_domain=claim.analysis_domain.value,
            claim_kind=claim.claim_kind.value,
            confidence=claim.confidence.value,
            importance=claim.importance.value,
            statement=claim.statement,
            evidence_count=len(claim.evidence_card_ids),
            domain_analysis_as_of=claim.domain_analysis_as_of,
        )
        for index, claim in enumerate(ordered, start=1)
    )
    return SynthesisClaimPack(
        research_question=research_question,
        analysis_as_of=analysis_as_of,
        company_name=company_name,
        items=items,
    )
