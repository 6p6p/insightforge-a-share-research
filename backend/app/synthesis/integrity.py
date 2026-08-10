"""Claim integrity gateway for synthesis input (stage 4D.1A, Gate 0).

`ClaimIntegrityGateway.verify_claim(session, claim_id)` 按 claim 的**真实
analysis_domain + claim_schema_version** dispatch 到 generic / Financial /
Macro / Valuation 完整性校验，返回 `VerifiedSynthesisClaim`。本阶段只
**接受已验证的 Claim** 作为综合输入——任何 Claim / domain 子表 / Evidence /
Calculation / Comparison 缺失、或 fingerprint 与真实 persisted provenance
重算结果不一致 → `SynthesisClaimIntegrityError`，**不自动 repair**。

**Gateway = thin facade**（spec E / Gate 0）：child artifact 的确定性完整性
一律委托各 domain service 的**公开** verify API，不把 domain replay 逻辑堆进
本文件：
- generic（business/event/risk，v1）→ `ClaimService.verify_claim_integrity`
  （Claim fields / Evidence links / Evidence company / policy / fingerprint）；
- macro（v4/v5/v6）→ `MacroClaimService.verify_claim_integrity`（version-aware
  replay：MacroTransmissionChain + transmission links + analysis_as_of +
  channel/effect/impact/time policy + transmission fingerprint + claim
  fingerprint）。legacy v4/v5 链无 persisted analysis_as_of →
  `SynthesisUnsupportedClaimSchema`（不反推 / 不 backfill）；evidence 晚于
  analysis_as_of → `SynthesisFutureEvidence`（spec O，不当作数据损坏）；
- financial（v2/v3）→ 每个 linked `FinancialCalculation` 走
  `FinancialCalculationService.verify_calculation_integrity`（重新验证
  formula / inputs / period / scope / result / fingerprint / provenance），
  再以 **verified calculation fingerprint** 重建 claim fingerprint；
- valuation（v7）→ 每个 linked Comparison 走
  `RelativeValuationComparisonService.verify_comparison_integrity`（完整重放
  peer / median / premium / date / formula / fingerprint / peer links），再以
  **verified comparison fingerprint** 重建 claim fingerprint。

禁止：只读 DB 中现成的 calculation_fingerprint / comparison_fingerprint 就
认为 child artifact 完整；复制任何 domain formula / transmission policy /
comparison policy / fingerprint 逻辑（只调用公开函数）。
"""

from datetime import date
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.claims.contracts import (
    CLAIM_SCHEMA_VERSION,
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.claims.errors import ClaimError
from app.claims.financial_contracts import (
    FINANCIAL_CLAIM_SCHEMA_VERSION,
    FINANCIAL_CLAIM_SCHEMA_VERSION_V2,
    compute_financial_claim_fingerprint,
)
from app.claims.macro_errors import (
    MacroClaimError,
    MacroClaimFutureEvidence,
    MacroClaimUnsupportedSchema,
)
from app.db.models.claim import ClaimModel
from app.financial.calculations.errors import FinancialCalculationError
from app.financial.calculations.service import FinancialCalculationService
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_financial_calculation_link_repository import (
    ClaimFinancialCalculationLinkRepository,
)
from app.repositories.claim_relative_valuation_comparison_link_repository import (
    ClaimRelativeValuationComparisonLinkRepository,
)
from app.repositories.claim_repository import ClaimRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.relative_valuation_claim_profile_repository import (
    RelativeValuationClaimProfileRepository,
)
from app.services.claim_service import ClaimService
from app.services.macro_claim_service import MacroClaimService
from app.synthesis.contracts import VerifiedSynthesisClaim
from app.synthesis.errors import (
    SynthesisClaimIntegrityError,
    SynthesisFutureEvidence,
    SynthesisUnsupportedClaimSchema,
)
from app.valuation.claim_contracts import (
    VALUATION_CLAIM_SCHEMA_VERSION,
    compute_valuation_claim_fingerprint,
)
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.errors import ValuationError

_RELATIONS = ("supports", "contradicts", "context")
_GENERIC_DOMAINS = frozenset({"business", "event", "risk"})
_SUPPORTED_FINANCIAL_VERSIONS = frozenset(
    {FINANCIAL_CLAIM_SCHEMA_VERSION, FINANCIAL_CLAIM_SCHEMA_VERSION_V2}
)


class ClaimIntegrityGateway:
    """按真实 domain + schema version dispatch 校验输入 Claim 的完整性（thin facade）。

    child artifact 的确定性完整性一律委托各 domain service 的**公开**
    verify API；本 gateway 只负责 dispatch 与把 domain 错误映射为 synthesis
    稳定错误分类。
    """

    def __init__(
        self,
        *,
        claim_service: ClaimService,
        macro_claim_service: MacroClaimService,
        financial_calculation_service: FinancialCalculationService,
        valuation_comparison_service: RelativeValuationComparisonService,
    ) -> None:
        self._claim_service = claim_service
        self._macro_claim_service = macro_claim_service
        self._financial_calculation_service = financial_calculation_service
        self._valuation_comparison_service = valuation_comparison_service

    async def verify_claim(self, session: AsyncSession, claim_id: UUID) -> VerifiedSynthesisClaim:
        claim = await ClaimRepository(session).get_by_id(claim_id)
        if claim is None:
            # spec N：不存在 SynthesisClaimNotFound——缺失 Claim 视为引用损坏。
            raise SynthesisClaimIntegrityError("input claim missing")
        domain = claim.analysis_domain
        if domain in _GENERIC_DOMAINS:
            return await self._verify_generic(session, claim)
        if domain == "financial":
            return await self._verify_financial(session, claim)
        if domain == "macro":
            return await self._verify_macro(session, claim)
        if domain == "valuation":
            return await self._verify_valuation(session, claim)
        raise SynthesisUnsupportedClaimSchema()

    # ------------------------------------------------------------------ 各 domain

    async def _verify_generic(
        self, session: AsyncSession, claim: ClaimModel
    ) -> VerifiedSynthesisClaim:
        """generic v1：委托 ClaimService.verify_claim_integrity（fields / links /
        evidence / policy / fingerprint），不复制任何 generic replay 逻辑。
        当前只支持 CLAIM_SCHEMA_VERSION；其他版本 → SynthesisUnsupportedClaimSchema。"""
        if claim.claim_schema_version != CLAIM_SCHEMA_VERSION:
            raise SynthesisUnsupportedClaimSchema()
        try:
            verified = await self._claim_service.verify_claim_integrity(session, claim.claim_id)
        except ClaimError as exc:
            raise SynthesisClaimIntegrityError("generic claim integrity failed") from exc
        if verified is None:
            raise SynthesisClaimIntegrityError("input claim missing")
        by_relation = await self._evidence_by_relation(session, claim.claim_id)
        return self._verified(verified, by_relation, domain_analysis_as_of=None)

    async def _verify_macro(
        self, session: AsyncSession, claim: ClaimModel
    ) -> VerifiedSynthesisClaim:
        """macro：委托 MacroClaimService.verify_claim_integrity（version-aware
        transmission + claim replay），不复制任何 Macro policy / fingerprint 逻辑。
        legacy v4/v5 链无 persisted analysis_as_of → SynthesisUnsupportedClaimSchema。"""
        try:
            verified = await self._macro_claim_service.verify_claim_integrity(
                session, claim.claim_id
            )
        except MacroClaimUnsupportedSchema:
            raise SynthesisUnsupportedClaimSchema() from None
        except MacroClaimFutureEvidence:
            # spec O：evidence 晚于域分析截止 → 未来证据（不当作数据损坏）。
            raise SynthesisFutureEvidence() from None
        except MacroClaimError as exc:
            raise SynthesisClaimIntegrityError("macro claim integrity failed") from exc
        if verified is None:
            raise SynthesisClaimIntegrityError("input claim missing")
        by_relation = await self._evidence_by_relation(session, claim.claim_id)
        return self._verified(
            verified.claim, by_relation, domain_analysis_as_of=verified.analysis_as_of
        )

    async def _verify_financial(
        self, session: AsyncSession, claim: ClaimModel
    ) -> VerifiedSynthesisClaim:
        if claim.claim_schema_version not in _SUPPORTED_FINANCIAL_VERSIONS:
            raise SynthesisUnsupportedClaimSchema()
        by_relation = await self._evidence_by_relation(session, claim.claim_id)
        calculation_ids = await self._linked_ids(
            session,
            claim.claim_id,
            ClaimFinancialCalculationLinkRepository,
            lambda link: link.calculation_id,
        )
        # 每个 linked FinancialCalculation 走真实 public integrity API（重新派生
        # formula / inputs / period / scope / result / fingerprint / provenance），
        # 禁止只信 DB 中现成的 calculation_fingerprint。
        calc_fingerprints: dict[UUID, str] = {}
        for calc_id in calculation_ids:
            try:
                verified_calc = (
                    await self._financial_calculation_service.verify_calculation_integrity(
                        session, calc_id
                    )
                )
            except FinancialCalculationError as exc:
                raise SynthesisClaimIntegrityError(
                    "financial calculation integrity failed"
                ) from exc
            if verified_calc is None:
                raise SynthesisClaimIntegrityError("financial calculation missing")
            calc_fingerprints[calc_id] = verified_calc.calculation_fingerprint

        calc_by_relation = await self._relation_ids(
            session,
            claim.claim_id,
            ClaimFinancialCalculationLinkRepository,
            lambda link: link.calculation_id,
        )
        entries = {
            relation: [
                {
                    "calculation_id": str(calc_id),
                    "calculation_fingerprint": calc_fingerprints[calc_id],
                }
                for calc_id in sorted(ids, key=str)
            ]
            for relation, ids in calc_by_relation.items()
        }
        fingerprint = compute_financial_claim_fingerprint(
            claim_schema_version=claim.claim_schema_version,
            company_id=claim.company_id,
            research_question=claim.research_question,
            statement=claim.statement,
            claim_kind=claim.claim_kind,
            confidence=claim.confidence,
            importance=claim.importance,
            analyst_name=claim.analyst_name,
            analyst_version=claim.analyst_version,
            analyst_model_id=claim.analyst_model_id,
            supports_evidence=by_relation["supports"],
            contradicts_evidence=by_relation["contradicts"],
            context_evidence=by_relation["context"],
            supports_calculations=entries["supports"],
            contradicts_calculations=entries["contradicts"],
            context_calculations=entries["context"],
        )
        if fingerprint != claim.claim_fingerprint:
            raise SynthesisClaimIntegrityError("financial claim fingerprint mismatch")
        return self._verified(claim, by_relation, domain_analysis_as_of=None)

    async def _verify_valuation(
        self, session: AsyncSession, claim: ClaimModel
    ) -> VerifiedSynthesisClaim:
        if claim.claim_schema_version != VALUATION_CLAIM_SCHEMA_VERSION:
            raise SynthesisUnsupportedClaimSchema()
        profile = await RelativeValuationClaimProfileRepository(session).get_by_claim(
            claim.claim_id
        )
        if profile is None:
            raise SynthesisClaimIntegrityError("valuation claim profile missing")
        by_relation = await self._evidence_by_relation(session, claim.claim_id)

        comparison_ids = await self._linked_ids(
            session,
            claim.claim_id,
            ClaimRelativeValuationComparisonLinkRepository,
            lambda link: link.comparison_id,
        )
        # 每个 linked Comparison 走真实 public integrity API（完整重放 peer /
        # median / premium / date / formula / fingerprint / peer links），禁止只信
        # DB 中现成的 comparison_fingerprint。
        comp_fingerprints: dict[UUID, str] = {}
        for comparison_id in comparison_ids:
            try:
                verified_comp = (
                    await self._valuation_comparison_service.verify_comparison_integrity(
                        session, comparison_id
                    )
                )
            except ValuationError as exc:
                raise SynthesisClaimIntegrityError("valuation comparison integrity failed") from exc
            if verified_comp is None:
                raise SynthesisClaimIntegrityError("valuation comparison missing")
            comp_fingerprints[comparison_id] = verified_comp.comparison_fingerprint

        # valuation 的 evidence entries 需要 evidence_fingerprint。
        card_repo = EvidenceCardRepository(session)
        ev_fingerprints: dict[UUID, str] = {}
        for card_id in {card_id for ids in by_relation.values() for card_id in ids}:
            card = await card_repo.get_by_id(card_id)
            if card is None:
                raise SynthesisClaimIntegrityError("valuation evidence missing")
            ev_fingerprints[card_id] = card.evidence_fingerprint

        comp_by_relation = await self._relation_ids(
            session,
            claim.claim_id,
            ClaimRelativeValuationComparisonLinkRepository,
            lambda link: link.comparison_id,
        )
        evidence_entries = {
            relation: [
                {
                    "evidence_card_id": str(card_id),
                    "evidence_fingerprint": ev_fingerprints[card_id],
                }
                for card_id in ids
            ]
            for relation, ids in by_relation.items()
        }
        comparison_entries = {
            relation: [
                {
                    "comparison_id": str(comparison_id),
                    "comparison_fingerprint": comp_fingerprints[comparison_id],
                }
                for comparison_id in sorted(ids, key=str)
            ]
            for relation, ids in comp_by_relation.items()
        }
        fingerprint = compute_valuation_claim_fingerprint(
            claim_schema_version=claim.claim_schema_version,
            profile_schema_version=profile.profile_schema_version,
            company_id=claim.company_id,
            research_question=claim.research_question,
            analysis_as_of=profile.analysis_as_of,
            statement=claim.statement,
            assessment=profile.assessment,
            confidence=claim.confidence,
            importance=claim.importance,
            analyst_name=claim.analyst_name,
            analyst_version=claim.analyst_version,
            analyst_model_id=claim.analyst_model_id,
            supports_evidence=evidence_entries["supports"],
            contradicts_evidence=evidence_entries["contradicts"],
            context_evidence=evidence_entries["context"],
            supports_comparisons=comparison_entries["supports"],
            contradicts_comparisons=comparison_entries["contradicts"],
            context_comparisons=comparison_entries["context"],
        )
        if fingerprint != claim.claim_fingerprint:
            raise SynthesisClaimIntegrityError("valuation claim fingerprint mismatch")
        return self._verified(claim, by_relation, domain_analysis_as_of=profile.analysis_as_of)

    # ------------------------------------------------------------------ 内部

    async def _evidence_by_relation(
        self, session: AsyncSession, claim_id: UUID
    ) -> dict[str, list[UUID]]:
        return await self._relation_ids(
            session,
            claim_id,
            ClaimEvidenceLinkRepository,
            lambda link: link.evidence_card_id,
        )

    async def _relation_ids(
        self,
        session: AsyncSession,
        claim_id: UUID,
        repo_cls,
        id_getter,
    ) -> dict[str, list[UUID]]:
        """按 relation 分组（supports/contradicts/context），组内 canonical 排序。

        与各 domain `_derive` 的 evidence_by_relation / *_by_relation 构造一致
        （sorted(key=str)）——fingerprint 重算必须逐字节匹配。
        """
        links = await repo_cls(session).list_by_claim(claim_id)
        by_relation: dict[str, list[UUID]] = {relation: [] for relation in _RELATIONS}
        for link in links:
            by_relation[link.relation].append(id_getter(link))
        return {relation: sorted(ids, key=str) for relation, ids in by_relation.items()}

    async def _linked_ids(
        self,
        session: AsyncSession,
        claim_id: UUID,
        repo_cls,
        id_getter,
    ) -> set[UUID]:
        """全部 linked id（跨 relation 去重，供一次性加载 fingerprint）。"""
        links = await repo_cls(session).list_by_claim(claim_id)
        return {id_getter(link) for link in links}

    @staticmethod
    def _verified(
        claim: ClaimModel,
        by_relation: dict[str, list[UUID]],
        *,
        domain_analysis_as_of: date | None,
    ) -> VerifiedSynthesisClaim:
        card_ids = sorted({card_id for ids in by_relation.values() for card_id in ids}, key=str)
        return VerifiedSynthesisClaim(
            claim_id=claim.claim_id,
            claim_fingerprint=claim.claim_fingerprint,
            company_id=claim.company_id,
            research_question_sha256=claim.research_question_sha256,
            analysis_domain=ClaimAnalysisDomain(claim.analysis_domain),
            claim_kind=ClaimKind(claim.claim_kind),
            statement=claim.statement,
            confidence=ClaimConfidence(claim.confidence),
            importance=ClaimImportance(claim.importance),
            claim_schema_version=claim.claim_schema_version,
            analyst_name=claim.analyst_name,
            analyst_version=claim.analyst_version,
            analyst_model_id=claim.analyst_model_id,
            evidence_card_ids=card_ids,
            domain_analysis_as_of=domain_analysis_as_of,
        )
