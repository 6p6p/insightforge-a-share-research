"""Evidence Pack builder (stage 4B.1): deterministic E1..En projection.

- 从真实 EvidenceCard（PG）构造最小投影，**不发送** DB UUID / fingerprint /
  locator_refs / RawArtifact / 完整 HTML/PDF / Chroma distance；
- 每条模型输入只含必要字段：evidence_ref / evidence_statement / evidence_type /
  origin_type / authority_tier / provider_key；document origin 可附 quote_text /
  source_published_at / reporting_period_end；
- 确定性 alias：按 str(evidence_card_id) 升序编号 E1..En → ref_to_card_id /
  card_id_to_ref 双向映射（ref resolution 与日志用）。
"""

from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from app.analysis.claims.contracts import EvidencePack, EvidencePackItem
from app.analysis.claims.errors import ClaimAnalysisEvidenceCompanyMismatch


@dataclass(frozen=True)
class EvidencePackSource:
    """EvidenceCard 在证据包中的最小来源投影（由 Service 从真实 PG 行映射）。

    构造纯 Python 对象即可（无需 SQLAlchemy session / DB），单元测试直接构造；
    `from_model` 从 EvidenceCardModel 行映射所需字段。
    """

    evidence_card_id: UUID
    evidence_statement: str
    evidence_type: str
    origin_type: str
    authority_tier_snapshot: int
    provider_key: str
    quote_text: str | None = None
    source_published_at: datetime | None = None
    reporting_period_end: date | None = None
    # V1.1 closure：确定性 importance 上限策略的输入（critical claim 需要
    # supports 证据 critical_claim_eligible=true；不进模型 prompt）。
    critical_claim_eligible: bool = False

    @classmethod
    def from_model(cls, card) -> "EvidencePackSource":
        """从真实 EvidenceCardModel 行映射为最小投影（只取必要字段）。"""
        return cls(
            evidence_card_id=card.evidence_card_id,
            evidence_statement=card.evidence_statement,
            evidence_type=card.evidence_type,
            origin_type=card.origin_type,
            authority_tier_snapshot=card.authority_tier_snapshot,
            provider_key=card.provider_key,
            quote_text=card.quote_text,
            source_published_at=card.source_published_at,
            reporting_period_end=card.reporting_period_end,
            critical_claim_eligible=card.critical_claim_eligible_snapshot,
        )


def build_evidence_pack(sources: list[EvidencePackSource]) -> EvidencePack:
    """构造确定性 Evidence Pack（E1..En 按 str(evidence_card_id) 升序）。

    - 空包 → ClaimAnalysisEvidenceCompanyMismatch（分析必须有证据）；
    - alias 编号稳定：同证据集合 → 相同 E1..En 映射，ref resolution 可复现。
    """
    if not sources:
        raise ClaimAnalysisEvidenceCompanyMismatch()
    ordered = sorted(sources, key=lambda source: str(source.evidence_card_id))
    items: list[EvidencePackItem] = []
    ref_to_card_id: dict[str, UUID] = {}
    card_id_to_ref: dict[UUID, str] = {}
    for index, source in enumerate(ordered, start=1):
        ref = f"E{index}"
        items.append(
            EvidencePackItem(
                evidence_ref=ref,
                evidence_statement=source.evidence_statement,
                evidence_type=source.evidence_type,
                origin_type=source.origin_type,
                authority_tier=int(source.authority_tier_snapshot),
                provider_key=source.provider_key,
                quote_text=source.quote_text,
                source_published_at=source.source_published_at,
                reporting_period_end=source.reporting_period_end,
            )
        )
        ref_to_card_id[ref] = source.evidence_card_id
        card_id_to_ref[source.evidence_card_id] = ref
    return EvidencePack(
        items=tuple(items),
        ref_to_card_id=ref_to_card_id,
        card_id_to_ref=card_id_to_ref,
    )
