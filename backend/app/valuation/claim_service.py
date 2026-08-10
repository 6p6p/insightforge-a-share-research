"""Relative valuation claim service (stage 4C.2B.1): provenance + persistence + replay.

`create_claim(draft)` / `create_claim_batch(drafts)` 把**引用已登记
RelativeValuationComparison** 的 Relative Valuation Claim 确定性登记为 Claim +
RelativeValuationClaimProfile + ClaimEvidenceLink（自动展开 source Evidence）+
ClaimRelativeValuationComparisonLink，形成 **Claim →
ClaimRelativeValuationComparisonLink → RelativeValuationComparison →
ValuationMetricObservation → EvidenceCard → Source** 完整可重算证据链，使
Audit 能重算 peer median / premium 并知道 judgment 基于哪些 peer comparisons。
**0 LLM / 0 Chroma / 0 Report / 0 Audit / 0 LangGraph**。

流程（两步提交结构，镜像 FinancialClaimService）：
1. 短 DB session 从真实 PG 加载全部 Comparison refs 并**逐条重放校验**（缺失 →
   ValuationClaimComparisonNotFound；company != draft →
   ValuationClaimComparisonMismatch；comparison.analysis_as_of !=
   draft.analysis_as_of → ValuationClaimAnalysisDateMismatch；重放损坏 →
   ValuationClaimIntegrityError，**不 repair**），再校验跨 comparison 一致性
   （全部同一 metric_as_of → ValuationClaimMetricDateMismatch；全部同一
   peer_company_id set → ValuationClaimPeerSetMismatch；metric_code 不重复 →
   ValuationClaimDuplicateMetric），加载 target / peer Observations 的 source
   Evidence（automatic expansion）+ additional Evidence（company 一致），随后
   立即关闭 connection（纯函数阶段不持有 DB 连接）。
2. 纯函数派生（无 DB）：
   - **automatic Evidence expansion**：每个 comparison 的 target Observation +
     全部 peer Observations 的 source Evidence 自动加入 ClaimEvidenceLinks，
     **一律 relation=context**（spec N）；跨 comparison 对 shared Evidence
     context 去重；
   - **additional Evidence**：保持 caller 指定的 supports/contradicts/context
     （spec O）；与 automatic context Evidence 冲突 →
     ValuationClaimRelationConflict（不静默选一个）；
   - **critical policy**：critical Claim 要求每个 support Comparison 的 target
     Observation + 全部 peer Observations 的 source Evidence **全部**
     critical_claim_eligible_snapshot=true（spec Q）；additional supports 不能
     替代；
   - **assessment**：ValuationClaimDraft 的分析判断，程序**不写 hidden
     thresholds**、不从 premium 自动推导（spec P）；
   - **v7 fingerprint**：claim_schema_version + profile_schema_version + claim
     semantic fields + assessment + analysis_as_of + comparison groups
     （comparison_id + comparison_fingerprint）+ evidence groups
     （evidence_card_id + evidence_fingerprint，含 automatic + additional）。
3. 短 DB transaction：create_or_get Claim（ON CONFLICT(claim_fingerprint)，无
   进程锁）→ created=True 时同事务插入 Profile + Comparison links + Evidence
   links（任一失败 → 整条 rollback，0 partial write）；created=False 时**重新
   加载 Claim / Profile / links / Comparisons / peers / Observations /
   EvidenceCards 并重新派生逐项核实**（任一损坏 → ValuationClaimIntegrityError，
   **不自动 repair**）。任何 SQLAlchemyError → 整条 rollback +
   ValuationClaimPersistenceFailed。并发 → 最终 1 Claim + 1 Profile + 1 套
   links。无 update API（修改 = 新 Claim = 新 fingerprint = 新行）。
   create_claim_batch 为 **all-drafts-validate-first + 单 transaction**（任一
   draft 校验失败 → 整批拒绝，0 写；items 按 input drafts 顺序返回）。

**不创建 Report / DraftSection / ReviewIssue**；不接 LangGraph 分析节点；不
修改 Financial / Macro Claims（各自 schema version 原样保留）。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.contracts import ClaimAnalysisDomain, ClaimKind, compute_research_question_sha256
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.claim_relative_valuation_comparison_link import (
    ClaimRelativeValuationComparisonLinkModel,
)
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.relative_valuation_claim_profile import RelativeValuationClaimProfileModel
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_relative_valuation_comparison_link_repository import (
    ClaimRelativeValuationComparisonLinkRepository,
)
from app.repositories.claim_repository import ClaimRepository
from app.repositories.relative_valuation_claim_profile_repository import (
    RelativeValuationClaimProfileRepository,
)
from app.valuation.claim_contracts import (
    MAX_VALUATION_CLAIMS_PER_BATCH,
    MAX_VALUATION_COMPARISONS_PER_CLAIM,
    VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
    VALUATION_CLAIM_SCHEMA_VERSION,
    ValuationClaimBatchItem,
    ValuationClaimBatchResult,
    ValuationClaimDraft,
    ValuationClaimImportance,
    ValuationClaimResult,
    compute_valuation_claim_fingerprint,
)
from app.valuation.claim_errors import (
    ValuationClaimAnalysisDateMismatch,
    ValuationClaimComparisonMismatch,
    ValuationClaimComparisonNotFound,
    ValuationClaimCriticalEvidenceInsufficient,
    ValuationClaimDraftError,
    ValuationClaimDuplicateMetric,
    ValuationClaimEvidenceCompanyMismatch,
    ValuationClaimIntegrityError,
    ValuationClaimMetricDateMismatch,
    ValuationClaimPeerSetMismatch,
    ValuationClaimPersistenceFailed,
    ValuationClaimRelationConflict,
)
from app.valuation.comparison_service import (
    RelativeValuationComparisonService,
    VerifiedComparison,
)
from app.valuation.errors import ValuationError

_RELATIONS = ("supports", "contradicts", "context")


@dataclass(frozen=True)
class _LoadedReferences:
    """加载并校验后的全部 Comparison / Evidence 引用（真实 PG 数据）。"""

    verified: dict[UUID, VerifiedComparison]  # comparison_id -> verified
    additional_evidence: dict[UUID, EvidenceCardModel]  # card_id -> card


@dataclass(frozen=True)
class _DerivedValuationClaim:
    """纯函数阶段派生的全部确定性值（fingerprint / links / 策略结果）。"""

    fingerprint: str
    question_sha256: str
    evidence_by_relation: dict[str, list[UUID]]  # relation -> sorted evidence ids
    evidence_fingerprints: dict[UUID, str]  # card_id -> evidence_fingerprint
    comparisons_by_relation: dict[str, list[UUID]]  # relation -> sorted comparison ids
    comparison_fingerprints: dict[UUID, str]  # comparison_id -> comparison_fingerprint


def _assign_relation(auto: dict[UUID, str], card_id: UUID, relation: str) -> None:
    """把 Evidence 归入 relation；已有不同 relation → ValuationClaimRelationConflict。

    - 同一 relation 重复（多个 Comparisons 共享同一 Evidence）→ 幂等去重；
    - 不同 relation 冲突 → 抛错（**不静默选一个**）。
    """
    current = auto.get(card_id)
    if current is None:
        auto[card_id] = relation
    elif current != relation:
        raise ValuationClaimRelationConflict()


class ValuationClaimService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_claim(self, draft: ValuationClaimDraft) -> "ValuationClaimResult":
        """登记一条引用已登记 Comparisons 的 Relative Valuation Claim（0 partial write）。"""
        batch = await self.create_claim_batch([draft])
        claim_id = batch.claim_ids[0]
        return ValuationClaimResult(
            claim_id=claim_id,
            claim_fingerprint=batch.fingerprints[claim_id],
            replayed=claim_id in batch.replayed,
        )

    async def create_claim_batch(
        self, drafts: list[ValuationClaimDraft]
    ) -> ValuationClaimBatchResult:
        """把 1..MAX_VALUATION_CLAIMS_PER_BATCH 条 Relative Valuation Claim 原子登记。

        两步提交结构（镜像 FinancialClaimService.create_claim_batch）：
        1. **all-drafts-validate-first**——开事务前，对全部 drafts 加载引用并完成
           派生（automatic expansion / relation semantics / critical policy /
           fingerprint）；任何一条失败 → 整批拒绝（0 写）；
        2. **单 transaction**——逐个 create_or_get + Profile + links / replay 校验；
           任一 SQLAlchemyError / ValuationClaimIntegrityError → 整批回滚，不留下
           半批 Claim（禁止 compensating delete）。
        items 按 input drafts 顺序返回（ordinal 一一对应）。
        """
        if not isinstance(drafts, list) or not (1 <= len(drafts) <= MAX_VALUATION_CLAIMS_PER_BATCH):
            raise ValuationClaimDraftError(f"drafts 必须在 1..{MAX_VALUATION_CLAIMS_PER_BATCH} 条")

        # 1. 短 DB session：一次性加载并校验全部 drafts 的 Comparison/Evidence 引用。
        async with self._sessionmaker() as session:
            loaded_list = [await self._load_validate_session(session, draft) for draft in drafts]

        # 2. 全部 drafts 先完成派生（任何一条失败 → 整批拒绝，0 写）。
        derived_list = [
            self._derive(draft, loaded) for draft, loaded in zip(drafts, loaded_list, strict=True)
        ]

        # 3. 单 transaction：逐个 create_or_get + Profile + links / replay。
        fingerprints: dict[UUID, str] = {}
        items: list[ValuationClaimBatchItem] = []
        async with self._sessionmaker() as session:
            try:
                repo = ClaimRepository(session)
                link_repo = ClaimEvidenceLinkRepository(session)
                comp_link_repo = ClaimRelativeValuationComparisonLinkRepository(session)
                profile_repo = RelativeValuationClaimProfileRepository(session)
                for ordinal, (draft, derived) in enumerate(
                    zip(drafts, derived_list, strict=True), start=1
                ):
                    existing = await repo.get_by_fingerprint(derived.fingerprint)
                    if existing is not None:
                        await self._verify_replay(session, existing, draft)
                        fingerprints[existing.claim_id] = derived.fingerprint
                        items.append(
                            ValuationClaimBatchItem(
                                ordinal=ordinal,
                                claim_id=existing.claim_id,
                                replayed=True,
                            )
                        )
                        continue

                    claim = ClaimModel(claim_id=uuid.uuid4(), **self._claim_kwargs(draft, derived))
                    persisted, created = await repo.create_or_get(claim)
                    if not created:
                        # 并发输家：复用既有 Claim（replay 校验后返回）。
                        await self._verify_replay(session, persisted, draft)
                        fingerprints[persisted.claim_id] = derived.fingerprint
                        items.append(
                            ValuationClaimBatchItem(
                                ordinal=ordinal,
                                claim_id=persisted.claim_id,
                                replayed=True,
                            )
                        )
                        continue

                    # 本事务创建了 Claim：同事务写 Profile + Comparison links +
                    # Evidence links（任一失败 → 整条 rollback，0 partial write）。
                    await profile_repo.create(
                        RelativeValuationClaimProfileModel(
                            claim_id=persisted.claim_id,
                            assessment=draft.assessment.value,
                            analysis_as_of=draft.analysis_as_of,
                            profile_schema_version=VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
                        )
                    )
                    await comp_link_repo.bulk_insert(
                        self._comparison_links(persisted.claim_id, derived)
                    )
                    await link_repo.bulk_insert(self._evidence_links(persisted.claim_id, derived))
                    fingerprints[persisted.claim_id] = derived.fingerprint
                    items.append(
                        ValuationClaimBatchItem(
                            ordinal=ordinal,
                            claim_id=persisted.claim_id,
                            replayed=False,
                        )
                    )
                await session.commit()
                return ValuationClaimBatchResult(items=tuple(items), fingerprints=fingerprints)
            except ValuationClaimIntegrityError:
                # replay 校验发现既有 Claim 数据损坏 → 显式回滚本事务，然后抛出。
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise ValuationClaimPersistenceFailed() from exc

    # ------------------------------------------------------------------ 加载校验

    async def _load_validate_session(
        self,
        session: AsyncSession,
        draft: ValuationClaimDraft,
    ) -> _LoadedReferences:
        """加载并校验全部 Comparison / Observations / Evidence（spec I-K, N, O）。

        - 每个 Comparison：缺失 → ValuationClaimComparisonNotFound；company
          != draft → ValuationClaimComparisonMismatch；comparison.analysis_as_of
          != draft.analysis_as_of → ValuationClaimAnalysisDateMismatch；重放损坏
          → ValuationClaimIntegrityError（包装自 ValuationError）；
        - 全部 selected comparisons 的 metric_as_of 必须相同 →
          ValuationClaimMetricDateMismatch；
        - 全部 selected comparisons 的 peer_company_id 集合必须完全相同 →
          ValuationClaimPeerSetMismatch（不做 silent intersection / union）；
        - 一个 claim 内 metric_code 不得重复（v1 最多 PE/PB/PS）→
          ValuationClaimDuplicateMetric；
        - additional Evidence：缺失 / company != draft →
          ValuationClaimEvidenceCompanyMismatch（peer company Evidence 不能作为
          target additional Evidence）。
        """
        comparison_svc = RelativeValuationComparisonService(self._sessionmaker)
        comparison_ids = (
            draft.support_comparison_ids
            + draft.contradict_comparison_ids
            + draft.context_comparison_ids
        )
        verified: dict[UUID, VerifiedComparison] = {}
        metric_codes: set[str] = set()
        metric_as_ofs: set[date] = set()
        peer_sets: set[frozenset[UUID]] = set()
        for comparison_id in comparison_ids:
            try:
                v = await comparison_svc.verify_comparison_integrity(session, comparison_id)
            except ValuationError as exc:
                raise ValuationClaimIntegrityError() from exc
            if v is None:
                raise ValuationClaimComparisonNotFound()
            if v.target_company_id != draft.company_id:
                raise ValuationClaimComparisonMismatch()
            if v.analysis_as_of != draft.analysis_as_of:
                raise ValuationClaimAnalysisDateMismatch()
            metric_codes.add(v.metric_code)
            metric_as_ofs.add(v.metric_as_of)
            peer_sets.add(frozenset(v.peer_companies))
            verified[comparison_id] = v

        if len(metric_as_ofs) > 1:
            raise ValuationClaimMetricDateMismatch()
        if len(peer_sets) > 1:
            raise ValuationClaimPeerSetMismatch()
        # metric_code 唯一（且 v1 最多 PE/PB/PS 三个 comparison；draft 层已限总
        # 数 <= 3，此处对真实 Comparison 的 metric 唯一性做最终校验）。
        if len(metric_codes) != len(comparison_ids):
            raise ValuationClaimDuplicateMetric()
        if len(comparison_ids) > MAX_VALUATION_COMPARISONS_PER_CLAIM:
            raise ValuationClaimDuplicateMetric(
                f"valuation claim 最多 {MAX_VALUATION_COMPARISONS_PER_CLAIM} 个 comparison"
            )

        additional_ids = (
            set(draft.additional_support_evidence_ids)
            | set(draft.additional_contradict_evidence_ids)
            | set(draft.additional_context_evidence_ids)
        )
        additional_evidence: dict[UUID, EvidenceCardModel] = {}
        if additional_ids:
            additional_evidence = await self._load_evidence_cards(
                session, additional_ids, draft.company_id
            )

        return _LoadedReferences(
            verified=verified,
            additional_evidence=additional_evidence,
        )

    @staticmethod
    async def _load_evidence_cards(
        session: AsyncSession,
        card_ids: set[UUID],
        company_id: UUID,
    ) -> dict[UUID, EvidenceCardModel]:
        """从真实 PG 加载 additional EvidenceCards；缺失 / 跨公司 →
        ValuationClaimEvidenceCompanyMismatch。"""
        if not card_ids:
            return {}
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
        )
        rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(card_ids):
            raise ValuationClaimEvidenceCompanyMismatch()
        for card in by_id.values():
            if card.company_id != company_id:
                raise ValuationClaimEvidenceCompanyMismatch()
        return by_id

    # ------------------------------------------------------------------ 纯函数派生

    def _derive(
        self,
        draft: ValuationClaimDraft,
        loaded: _LoadedReferences,
    ) -> _DerivedValuationClaim:
        """纯函数派生：automatic expansion → additional → critical → fingerprint。

        1. automatic Evidence expansion（spec N）：每个 comparison 的 target
           Observation + 全部 peer Observations 的 source Evidence 自动加入
           ClaimEvidenceLinks，**一律 relation=context**；跨 comparison 对
           shared Evidence context 去重。
        2. additional Evidence（spec O）：保持 caller 指定的 relation；与
           automatic context Evidence 冲突 → ValuationClaimRelationConflict。
        3. critical policy（spec Q）：critical Claim 要求每个 support Comparison
           的 target Observation + 全部 peer Observations 的 source Evidence
           **全部** critical_claim_eligible_snapshot=true；additional supports
           不能替代。
        4. assessment（spec P）：ValuationClaimDraft 的分析判断，程序不写 hidden
           thresholds、不从 premium 自动推导。
        5. v7 fingerprint（spec R）。
        """
        auto: dict[UUID, str] = {}
        comparison_ids_by_relation = {
            "supports": draft.support_comparison_ids,
            "contradicts": draft.contradict_comparison_ids,
            "context": draft.context_comparison_ids,
        }
        for comparison_ids in comparison_ids_by_relation.values():
            for comparison_id in comparison_ids:
                for card_id in loaded.verified[comparison_id].evidence:
                    _assign_relation(auto, card_id, "context")

        additional_by_relation = {
            "supports": draft.additional_support_evidence_ids,
            "contradicts": draft.additional_contradict_evidence_ids,
            "context": draft.additional_context_evidence_ids,
        }
        for relation, card_ids in additional_by_relation.items():
            for card_id in card_ids:
                _assign_relation(auto, card_id, relation)

        evidence_by_relation: dict[str, list[UUID]] = {
            relation: sorted(
                (card_id for card_id, rel in auto.items() if rel == relation),
                key=str,
            )
            for relation in _RELATIONS
        }

        # evidence fingerprints（供 v7 fingerprint / replay）：全部 auto +
        # additional 卡片的 evidence_fingerprint。
        cards: dict[UUID, EvidenceCardModel] = {}
        for verified in loaded.verified.values():
            cards.update(verified.evidence)
        cards.update(loaded.additional_evidence)
        evidence_fingerprints = {card_id: cards[card_id].evidence_fingerprint for card_id in auto}

        # critical policy（spec Q）：每个 support Comparison 的完整 source
        # Evidence（target + 全部 peers）必须全部 eligible。
        if draft.importance == ValuationClaimImportance.CRITICAL:
            for comparison_id in draft.support_comparison_ids:
                for card in loaded.verified[comparison_id].evidence.values():
                    if not card.critical_claim_eligible_snapshot:
                        raise ValuationClaimCriticalEvidenceInsufficient()

        comparisons_by_relation: dict[str, list[UUID]] = {
            relation: sorted(comparison_ids, key=str)
            for relation, comparison_ids in comparison_ids_by_relation.items()
        }
        comparison_fingerprints = {
            comparison_id: loaded.verified[comparison_id].comparison_fingerprint
            for comparison_id in loaded.verified
        }

        fingerprint = compute_valuation_claim_fingerprint(
            claim_schema_version=VALUATION_CLAIM_SCHEMA_VERSION,
            profile_schema_version=VALUATION_CLAIM_PROFILE_SCHEMA_VERSION,
            company_id=draft.company_id,
            research_question=draft.research_question,
            analysis_as_of=draft.analysis_as_of,
            statement=draft.statement,
            assessment=draft.assessment.value,
            confidence=draft.confidence.value,
            importance=draft.importance.value,
            analyst_name=draft.analyst_name,
            analyst_version=draft.analyst_version,
            analyst_model_id=draft.analyst_model_id,
            supports_evidence=self._evidence_entries(
                evidence_by_relation["supports"], evidence_fingerprints
            ),
            contradicts_evidence=self._evidence_entries(
                evidence_by_relation["contradicts"], evidence_fingerprints
            ),
            context_evidence=self._evidence_entries(
                evidence_by_relation["context"], evidence_fingerprints
            ),
            supports_comparisons=self._comparison_entries(
                comparisons_by_relation["supports"], comparison_fingerprints
            ),
            contradicts_comparisons=self._comparison_entries(
                comparisons_by_relation["contradicts"], comparison_fingerprints
            ),
            context_comparisons=self._comparison_entries(
                comparisons_by_relation["context"], comparison_fingerprints
            ),
        )
        return _DerivedValuationClaim(
            fingerprint=fingerprint,
            question_sha256=compute_research_question_sha256(draft.research_question),
            evidence_by_relation=evidence_by_relation,
            evidence_fingerprints=evidence_fingerprints,
            comparisons_by_relation=comparisons_by_relation,
            comparison_fingerprints=comparison_fingerprints,
        )

    @staticmethod
    def _evidence_entries(card_ids: list[UUID], fingerprints: dict[UUID, str]) -> list[dict]:
        return [
            {"evidence_card_id": str(card_id), "evidence_fingerprint": fingerprints[card_id]}
            for card_id in card_ids
        ]

    @staticmethod
    def _comparison_entries(
        comparison_ids: list[UUID], fingerprints: dict[UUID, str]
    ) -> list[dict]:
        return [
            {
                "comparison_id": str(comparison_id),
                "comparison_fingerprint": fingerprints[comparison_id],
            }
            for comparison_id in comparison_ids
        ]

    # ------------------------------------------------------------------ 持久化

    @staticmethod
    def _claim_kwargs(draft: ValuationClaimDraft, derived: _DerivedValuationClaim) -> dict:
        return {
            "company_id": draft.company_id,
            "research_question": draft.research_question,
            "research_question_sha256": derived.question_sha256,
            "statement": draft.statement,
            "analysis_domain": ClaimAnalysisDomain.VALUATION.value,
            "claim_kind": ClaimKind.RELATIVE_VALUATION.value,
            "confidence": draft.confidence.value,
            "importance": draft.importance.value,
            "analyst_name": draft.analyst_name,
            "analyst_version": draft.analyst_version,
            "analyst_model_id": draft.analyst_model_id,
            "claim_schema_version": VALUATION_CLAIM_SCHEMA_VERSION,
            "claim_fingerprint": derived.fingerprint,
        }

    @staticmethod
    def _evidence_links(
        claim_id: UUID, derived: _DerivedValuationClaim
    ) -> list[ClaimEvidenceLinkModel]:
        links: list[ClaimEvidenceLinkModel] = []
        for relation in _RELATIONS:
            for card_id in derived.evidence_by_relation[relation]:
                links.append(
                    ClaimEvidenceLinkModel(
                        claim_id=claim_id,
                        evidence_card_id=card_id,
                        relation=relation,
                    )
                )
        return links

    @staticmethod
    def _comparison_links(
        claim_id: UUID, derived: _DerivedValuationClaim
    ) -> list[ClaimRelativeValuationComparisonLinkModel]:
        links: list[ClaimRelativeValuationComparisonLinkModel] = []
        for relation in _RELATIONS:
            for comparison_id in derived.comparisons_by_relation[relation]:
                links.append(
                    ClaimRelativeValuationComparisonLinkModel(
                        claim_id=claim_id,
                        comparison_id=comparison_id,
                        relation=relation,
                    )
                )
        return links

    # ------------------------------------------------------------------ replay

    async def _verify_replay(
        self,
        session: AsyncSession,
        existing: ClaimModel,
        draft: ValuationClaimDraft,
    ) -> None:
        """已有 fingerprint 的 valuation Claim replay 完整性校验（spec U）。

        重新加载 Claim / Profile / comparison links / evidence links /
        Comparisons / peers / Observations / EvidenceCards，重新执行 Comparison
        integrity、automatic Evidence expansion、relation semantics / critical
        policy / relation conflict、v7 fingerprint，逐项核实。任一损坏 →
        ValuationClaimIntegrityError，**不自动 repair**。
        """
        loaded = await self._load_validate_session(session, draft)
        try:
            derived = self._derive(draft, loaded)
        except (
            ValuationClaimCriticalEvidenceInsufficient,
            ValuationClaimRelationConflict,
        ) as exc:
            raise ValuationClaimIntegrityError(
                "valuation claim replay integrity check failed on policy/relation"
            ) from exc

        comp_links = await ClaimRelativeValuationComparisonLinkRepository(session).list_by_claim(
            existing.claim_id
        )
        actual_comp = {
            relation: sorted(
                (link.comparison_id for link in comp_links if link.relation == relation),
                key=str,
            )
            for relation in _RELATIONS
        }
        for relation in _RELATIONS:
            if actual_comp[relation] != derived.comparisons_by_relation[relation]:
                raise ValuationClaimIntegrityError(
                    f"valuation claim replay integrity check failed on comparison links[{relation}]"
                )

        ev_links = await ClaimEvidenceLinkRepository(session).list_by_claim(existing.claim_id)
        actual_ev = {
            relation: sorted(
                (link.evidence_card_id for link in ev_links if link.relation == relation),
                key=str,
            )
            for relation in _RELATIONS
        }
        for relation in _RELATIONS:
            if actual_ev[relation] != derived.evidence_by_relation[relation]:
                raise ValuationClaimIntegrityError(
                    f"valuation claim replay integrity check failed on links[{relation}]"
                )

        profile = await RelativeValuationClaimProfileRepository(session).get_by_claim(
            existing.claim_id
        )
        if profile is None:
            raise ValuationClaimIntegrityError(
                "valuation claim replay integrity check failed on profile missing"
            )
        if profile.assessment != draft.assessment.value:
            raise ValuationClaimIntegrityError(
                "valuation claim replay integrity check failed on profile.assessment"
            )
        if profile.analysis_as_of != draft.analysis_as_of:
            raise ValuationClaimIntegrityError(
                "valuation claim replay integrity check failed on profile.analysis_as_of"
            )
        if profile.profile_schema_version != VALUATION_CLAIM_PROFILE_SCHEMA_VERSION:
            raise ValuationClaimIntegrityError(
                "valuation claim replay integrity check failed on profile.profile_schema_version"
            )

        pairs = (
            ("company_id", existing.company_id, draft.company_id),
            ("research_question", existing.research_question, draft.research_question),
            (
                "research_question_sha256",
                existing.research_question_sha256,
                derived.question_sha256,
            ),
            ("statement", existing.statement, draft.statement),
            ("analysis_domain", existing.analysis_domain, ClaimAnalysisDomain.VALUATION.value),
            ("claim_kind", existing.claim_kind, ClaimKind.RELATIVE_VALUATION.value),
            ("confidence", existing.confidence, draft.confidence.value),
            ("importance", existing.importance, draft.importance.value),
            ("analyst_name", existing.analyst_name, draft.analyst_name),
            ("analyst_version", existing.analyst_version, draft.analyst_version),
            ("analyst_model_id", existing.analyst_model_id, draft.analyst_model_id),
            (
                "claim_schema_version",
                existing.claim_schema_version,
                VALUATION_CLAIM_SCHEMA_VERSION,
            ),
            ("claim_fingerprint", existing.claim_fingerprint, derived.fingerprint),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise ValuationClaimIntegrityError(
                    f"valuation claim replay integrity check failed on {name}"
                )
