"""Financial analyst importance downgrade tests (Final Autonomous Research).

critical claim 的 supports Calculation 的 source Evidence 与 additional
supports 全部 non-eligible → 确定性降级 normal（不失败、不提升）。
"""

from uuid import uuid4

from app.analysis.claims.evidence_pack import EvidencePackSource
from app.analysis.financial.packs import ResolvedFinancialClaim
from app.analysis.financial.service import FinancialAnalysisService
from app.claims.financial_contracts import FinancialClaimImportance


def _claim(
    importance: FinancialClaimImportance, supports: tuple | None = None
) -> ResolvedFinancialClaim:
    if supports is None:
        supports = (uuid4(),)
    return ResolvedFinancialClaim(
        statement="公司盈利能力有所提升",
        claim_kind="inference",
        confidence="medium",
        importance=importance,
        supports_calculations=tuple(supports),
        contradicts_calculations=(),
        context_calculations=(),
        additional_supports=(),
        additional_contradicts=(),
        additional_context=(),
    )


def _evidence(eligible: bool) -> EvidencePackSource:
    return EvidencePackSource(
        evidence_card_id=uuid4(),
        evidence_statement="营业收入同比增长",
        evidence_type="metric",
        origin_type="financial_extraction",
        authority_tier_snapshot=3,
        provider_key="eastmoney",
        critical_claim_eligible=eligible,
    )


def test_critical_without_eligible_downgraded_to_normal() -> None:
    service = FinancialAnalysisService.__new__(FinancialAnalysisService)  # type: ignore[arg-type]
    calc_id = uuid4()
    resolved = [_claim(FinancialClaimImportance.CRITICAL, supports=(calc_id,))]

    out = service._downgrade_importance(
        resolved,
        eligible_by_calc={calc_id: False},
        evidence_sources=[_evidence(eligible=False)],
    )

    assert out[0].importance == FinancialClaimImportance.NORMAL


def test_critical_with_eligible_calc_kept() -> None:
    service = FinancialAnalysisService.__new__(FinancialAnalysisService)  # type: ignore[arg-type]
    calc_id = uuid4()
    resolved = [_claim(FinancialClaimImportance.CRITICAL, supports=(calc_id,))]

    out = service._downgrade_importance(
        resolved,
        eligible_by_calc={calc_id: True},
        evidence_sources=[],
    )

    assert out[0].importance == FinancialClaimImportance.CRITICAL


def test_normal_claims_untouched() -> None:
    service = FinancialAnalysisService.__new__(FinancialAnalysisService)  # type: ignore[arg-type]
    resolved = [_claim(FinancialClaimImportance.NORMAL)]

    out = service._downgrade_importance(resolved, eligible_by_calc={}, evidence_sources=[])

    assert out[0].importance == FinancialClaimImportance.NORMAL
