"""Claim integrity gateway for synthesis input (stage 4D.1A).

`ClaimIntegrityGateway.verify_claim(session, claim_id)` 按 claim 的**真实
analysis_domain + claim_schema_version** dispatch 到 generic / Financial /
Macro / Valuation 完整性校验，返回 `VerifiedSynthesisClaim`。本阶段只
**接受已验证的 Claim** 作为综合输入——任何 Claim / domain 子表 / Evidence /
Calculation / Comparison 缺失、或 fingerprint 与真实 persisted provenance
重算结果不一致 → `SynthesisClaimIntegrityError`，**不自动 repair**。

**为何不调用各 domain service 的 private `_verify_replay`**：replay 校验需要
重建 semantic draft，而 automatic vs additional Evidence 在 claim_evidence_links
中不可区分（DB 层无此标记）。本 gateway 改为：从 persisted links + domain 子表
重建 fingerprint 输入 → 调用各 domain 的**公开** `compute_*_fingerprint` → 对比
claim.claim_fingerprint。这是唯一能处理含 automatic Evidence 的历史 Claim 的
方案。**禁止复制** FinancialCalculation formula / Macro transmission policy /
Valuation comparison policy / Claim fingerprint 逻辑本身（只调用公开函数）。

各 domain 重建规则（与各 domain service `_derive` 的 fingerprint 输入一致）：
- generic（business/event/risk，v1）：evidence links 按 relation 分组 →
  compute_claim_fingerprint；
- financial（v2/v3）：evidence links 分组 + claim_financial_calculation_links
  分组 → financial_calculations.calculation_fingerprint → dict entries →
  compute_financial_claim_fingerprint；
- macro（v4/v5/v6）：macro_transmission_chains.transmission_fingerprint +
  links 分组，additional_context = context − transmission_ids
  （transmission_ids 从 macro_transmission_evidence_links 读）→
  compute_macro_claim_fingerprint。legacy v1/v2 链 analysis_as_of 为 NULL →
  `SynthesisUnsupportedClaimSchema`（公开 compute 函数必填 date，且无 temporal
  语义）；
- valuation（v7）：links 分组 + evidence_cards.evidence_fingerprint +
  comparison links 分组 + relative_valuation_comparisons.comparison_fingerprint
  + profile（assessment / analysis_as_of / profile_schema_version）→ dict
  entries → compute_valuation_claim_fingerprint。
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
    compute_claim_fingerprint,
)
from app.claims.financial_contracts import (
    FINANCIAL_CLAIM_SCHEMA_VERSION,
    FINANCIAL_CLAIM_SCHEMA_VERSION_V2,
    compute_financial_claim_fingerprint,
)
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_CLAIM_SCHEMA_VERSION_V4,
    MACRO_CLAIM_SCHEMA_VERSION_V5,
    compute_macro_claim_fingerprint,
)
from app.db.models.claim import ClaimModel
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_financial_calculation_link_repository import (
    ClaimFinancialCalculationLinkRepository,
)
from app.repositories.claim_relative_valuation_comparison_link_repository import (
    ClaimRelativeValuationComparisonLinkRepository,
)
from app.repositories.claim_repository import ClaimRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.financial_calculation_repository import FinancialCalculationRepository
from app.repositories.macro_transmission_evidence_link_repository import (
    MacroTransmissionEvidenceLinkRepository,
)
from app.repositories.macro_transmission_repository import MacroTransmissionRepository
from app.repositories.relative_valuation_claim_profile_repository import (
    RelativeValuationClaimProfileRepository,
)
from app.repositories.relative_valuation_comparison_repository import (
    RelativeValuationComparisonRepository,
)
from app.synthesis.contracts import VerifiedSynthesisClaim
from app.synthesis.errors import (
    SynthesisClaimIntegrityError,
    SynthesisUnsupportedClaimSchema,
)
from app.valuation.claim_contracts import (
    VALUATION_CLAIM_SCHEMA_VERSION,
    compute_valuation_claim_fingerprint,
)

_RELATIONS = ("supports", "contradicts", "context")
_GENERIC_DOMAINS = frozenset({"business", "event", "risk"})
_SUPPORTED_FINANCIAL_VERSIONS = frozenset(
    {FINANCIAL_CLAIM_SCHEMA_VERSION, FINANCIAL_CLAIM_SCHEMA_VERSION_V2}
)
_SUPPORTED_MACRO_VERSIONS = frozenset(
    {
        MACRO_CLAIM_SCHEMA_VERSION,
        MACRO_CLAIM_SCHEMA_VERSION_V5,
        MACRO_CLAIM_SCHEMA_VERSION_V4,
    }
)


class ClaimIntegrityGateway:
    """按真实 domain + schema version dispatch 校验输入 Claim 的完整性。"""

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
        if claim.claim_schema_version != CLAIM_SCHEMA_VERSION:
            raise SynthesisUnsupportedClaimSchema()
        by_relation = await self._evidence_by_relation(session, claim.claim_id)
        fingerprint = compute_claim_fingerprint(
            claim_schema_version=claim.claim_schema_version,
            company_id=claim.company_id,
            research_question=claim.research_question,
            statement=claim.statement,
            analysis_domain=claim.analysis_domain,
            claim_kind=claim.claim_kind,
            confidence=claim.confidence,
            importance=claim.importance,
            analyst_name=claim.analyst_name,
            analyst_version=claim.analyst_version,
            analyst_model_id=claim.analyst_model_id,
            supports=by_relation["supports"],
            contradicts=by_relation["contradicts"],
            context=by_relation["context"],
        )
        if fingerprint != claim.claim_fingerprint:
            raise SynthesisClaimIntegrityError("generic claim fingerprint mismatch")
        return self._verified(claim, by_relation, domain_analysis_as_of=None)

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
        calc_repo = FinancialCalculationRepository(session)
        calc_fingerprints: dict[UUID, str] = {}
        for calc_id in calculation_ids:
            calc = await calc_repo.get_by_id(calc_id)
            if calc is None:
                raise SynthesisClaimIntegrityError("financial calculation missing")
            calc_fingerprints[calc_id] = calc.calculation_fingerprint

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

    async def _verify_macro(
        self, session: AsyncSession, claim: ClaimModel
    ) -> VerifiedSynthesisClaim:
        if claim.claim_schema_version not in _SUPPORTED_MACRO_VERSIONS:
            raise SynthesisUnsupportedClaimSchema()
        chain = await MacroTransmissionRepository(session).get_by_claim_id(claim.claim_id)
        if chain is None:
            raise SynthesisClaimIntegrityError("macro transmission chain missing")
        if chain.analysis_as_of is None:
            # legacy v1/v2 链无 analysis_as_of 查询列 → 公开 compute 函数必填
            # date，fingerprint 无法重算且无 temporal 语义 → 明确拒绝，不猜测。
            raise SynthesisUnsupportedClaimSchema()
        trans_links = await MacroTransmissionEvidenceLinkRepository(session).list_by_transmission(
            chain.transmission_id
        )
        transmission_ids = {link.evidence_card_id for link in trans_links}
        by_relation = await self._evidence_by_relation(session, claim.claim_id)
        additional_context = [
            card_id for card_id in by_relation["context"] if card_id not in transmission_ids
        ]
        fingerprint = compute_macro_claim_fingerprint(
            claim_schema_version=claim.claim_schema_version,
            company_id=claim.company_id,
            research_question=claim.research_question,
            analysis_as_of=chain.analysis_as_of,
            statement=claim.statement,
            claim_kind=claim.claim_kind,
            confidence=claim.confidence,
            importance=claim.importance,
            analyst_name=claim.analyst_name,
            analyst_version=claim.analyst_version,
            analyst_model_id=claim.analyst_model_id,
            transmission_fingerprint=chain.transmission_fingerprint,
            additional_supports=by_relation["supports"],
            additional_contradicts=by_relation["contradicts"],
            additional_context=additional_context,
        )
        if fingerprint != claim.claim_fingerprint:
            raise SynthesisClaimIntegrityError("macro claim fingerprint mismatch")
        return self._verified(claim, by_relation, domain_analysis_as_of=chain.analysis_as_of)

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
        comp_repo = RelativeValuationComparisonRepository(session)
        comp_fingerprints: dict[UUID, str] = {}
        for comparison_id in comparison_ids:
            comp = await comp_repo.get_by_id(comparison_id)
            if comp is None:
                raise SynthesisClaimIntegrityError("valuation comparison missing")
            comp_fingerprints[comparison_id] = comp.comparison_fingerprint

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
