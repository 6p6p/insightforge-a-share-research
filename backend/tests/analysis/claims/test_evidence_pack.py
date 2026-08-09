"""Evidence Pack builder unit tests (stage 4B.1)。

校验：
- E1..En 按 str(evidence_card_id) 升序编号（与调用方提交顺序无关，确定性）；
- ref_to_card_id / card_id_to_ref 双向映射完整；
- 空包 → ClaimAnalysisEvidenceCompanyMismatch；
- EvidencePackSource.from_model 只取必要字段（不发送 UUID / locator / raw /
  fingerprint / Chroma）。

**零真实 LLM / 零 DB**：全部只构造 EvidencePackSource。
"""

from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from app.analysis.claims.contracts import EvidencePackItem
from app.analysis.claims.errors import ClaimAnalysisEvidenceCompanyMismatch
from app.analysis.claims.evidence_pack import EvidencePackSource, build_evidence_pack

_E_A = UUID("aaaaaaaa-1111-1111-1111-111111111111")
_E_B = UUID("bbbbbbbb-2222-2222-2222-222222222222")
_E_C = UUID("cccccccc-3333-3333-3333-333333333333")


def _source(card_id: UUID, statement: str = "陈述", **overrides) -> EvidencePackSource:
    values = dict(
        evidence_card_id=card_id,
        evidence_statement=statement,
        evidence_type="metric",
        origin_type="document_chunk",
        authority_tier_snapshot=3,
        provider_key="xinhuanet",
    )
    values.update(overrides)
    return EvidencePackSource(**values)


def test_pack_assigns_e_refs_in_canonical_uuid_order() -> None:
    # 故意乱序提交：E1 必须是最小 str(uuid)，与提交顺序无关。
    pack = build_evidence_pack([_source(_E_C), _source(_E_A), _source(_E_B)])
    assert [item.evidence_ref for item in pack.items] == ["E1", "E2", "E3"]
    assert pack.ref_to_card_id["E1"] == _E_A
    assert pack.ref_to_card_id["E2"] == _E_B
    assert pack.ref_to_card_id["E3"] == _E_C
    assert pack.card_id_to_ref[_E_A] == "E1"
    assert pack.card_id_to_ref[_E_B] == "E2"
    assert pack.card_id_to_ref[_E_C] == "E3"


def test_pack_is_deterministic_for_same_evidence_set() -> None:
    a = build_evidence_pack([_source(_E_B), _source(_E_A)])
    b = build_evidence_pack([_source(_E_A), _source(_E_B)])
    assert a.ref_to_card_id == b.ref_to_card_id
    assert [i.evidence_statement for i in a.items] == [i.evidence_statement for i in b.items]


def test_empty_pack_rejected() -> None:
    with pytest.raises(ClaimAnalysisEvidenceCompanyMismatch):
        build_evidence_pack([])


def test_item_projects_minimal_fields() -> None:
    item = _source(
        _E_A,
        quote_text="海外收入同比增长31.4%",
        source_published_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
        reporting_period_end=date(2024, 12, 31),
    )
    pack = build_evidence_pack([item])
    projected: EvidencePackItem = pack.items[0]
    assert projected.evidence_ref == "E1"
    assert projected.evidence_statement == "陈述"
    assert projected.evidence_type == "metric"
    assert projected.origin_type == "document_chunk"
    assert projected.authority_tier == 3
    assert projected.provider_key == "xinhuanet"
    assert projected.quote_text == "海外收入同比增长31.4%"
    assert projected.source_published_at == datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
    assert projected.reporting_period_end == date(2024, 12, 31)


def test_item_does_not_contain_internal_fields() -> None:
    fields = set(EvidencePackItem.__dataclass_fields__)
    for forbidden in (
        "evidence_card_id",
        "locator_refs",
        "raw_content",
        "fingerprint",
        "distance",
        "company_id",
    ):
        assert forbidden not in fields
