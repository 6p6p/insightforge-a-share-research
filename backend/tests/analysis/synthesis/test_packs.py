"""Deterministic claim pack unit tests (stage 4D.1B).

验证：
- C alias 按 analysis_domain + str(claim_id) canonical 排序分配（C1..Cn）；
- alias_map() → C alias → 真实 claim_id（仅服务层 ref resolution 用）；
- ClaimPackItem 是**最小投影**：无 claim_fingerprint / research_question_sha256 /
  evidence_card_ids（LLM 永不看内部标识）。
"""

from datetime import date
from uuid import UUID, uuid4

from app.analysis.synthesis.packs import build_claim_pack
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.synthesis.contracts import VerifiedSynthesisClaim

_CUTOFF = date(2026, 8, 10)


def _claim(
    claim_id: UUID,
    *,
    domain: ClaimAnalysisDomain,
    kind: ClaimKind = ClaimKind.FACT,
) -> VerifiedSynthesisClaim:
    return VerifiedSynthesisClaim(
        claim_id=claim_id,
        claim_fingerprint="0" * 64,
        company_id=uuid4(),
        research_question_sha256="1" * 64,
        analysis_domain=domain,
        claim_kind=kind,
        statement="陈述",
        confidence=ClaimConfidence.HIGH,
        importance=ClaimImportance.NORMAL,
        claim_schema_version=1,
        analyst_name="test-analyst",
        analyst_version=1,
        analyst_model_id=None,
        evidence_card_ids=[],
        domain_analysis_as_of=None,
    )


class TestBuildClaimPack:
    def test_aliases_are_canonical_sorted_by_domain_then_claim_id(self) -> None:
        # 故意乱序：macro 的 id 小于 valuation，business 介于中间 → 排序后
        # business < macro < valuation（域优先），域内按 str(claim_id)。
        val_claim_id = uuid4()
        biz_a = uuid4()
        biz_b = uuid4()
        macro_id = uuid4()
        pack = build_claim_pack(
            research_question="q",
            analysis_as_of=_CUTOFF,
            company_name="贵州茅台",
            claims=[
                _claim(val_claim_id, domain=ClaimAnalysisDomain.VALUATION),
                _claim(biz_b, domain=ClaimAnalysisDomain.BUSINESS),
                _claim(macro_id, domain=ClaimAnalysisDomain.MACRO),
                _claim(biz_a, domain=ClaimAnalysisDomain.BUSINESS),
            ],
        )
        # 期望顺序：business 组（str 升序）→ macro → valuation。
        business_ids = sorted([biz_a, biz_b], key=str)
        expected = business_ids + [macro_id, val_claim_id]
        assert [item.alias for item in pack.items] == ["C1", "C2", "C3", "C4"]
        assert [item.claim_id for item in pack.items] == expected

    def test_alias_map_maps_back_to_claim_id(self) -> None:
        ids = [uuid4(), uuid4(), uuid4()]
        pack = build_claim_pack(
            research_question="q",
            analysis_as_of=_CUTOFF,
            company_name="贵州茅台",
            claims=[
                _claim(ids[0], domain=ClaimAnalysisDomain.BUSINESS),
                _claim(ids[1], domain=ClaimAnalysisDomain.MACRO),
                _claim(ids[2], domain=ClaimAnalysisDomain.VALUATION),
            ],
        )
        alias_map = pack.alias_map()
        assert len(alias_map) == 3
        # business / macro / valuation → 域序 C1/C2/C3。
        assert alias_map["C1"] == ids[0]
        assert alias_map["C2"] == ids[1]
        assert alias_map["C3"] == ids[2]

    def test_items_expose_only_minimal_projection(self) -> None:
        claim_id = uuid4()
        pack = build_claim_pack(
            research_question="q",
            analysis_as_of=_CUTOFF,
            company_name="贵州茅台",
            claims=[_claim(claim_id, domain=ClaimAnalysisDomain.BUSINESS)],
        )
        item = pack.items[0]
        assert item.alias == "C1"
        assert item.analysis_domain == "business"
        assert item.claim_kind == "fact"
        assert item.statement == "陈述"
        assert item.evidence_count == 0
        # 最小投影：LLM 可见字段不含 fingerprint / sha256 / evidence ids。
        for field in (
            "claim_fingerprint",
            "research_question_sha256",
            "evidence_card_ids",
        ):
            assert not hasattr(item, field)

    def test_company_name_and_cutoff_projected(self) -> None:
        pack = build_claim_pack(
            research_question="q",
            analysis_as_of=_CUTOFF,
            company_name="贵州茅台",
            claims=[_claim(uuid4(), domain=ClaimAnalysisDomain.BUSINESS)],
        )
        assert pack.company_name == "贵州茅台"
        assert pack.analysis_as_of == _CUTOFF
        assert pack.research_question == "q"
