"""Macro claim service (stage 4C.1A): transmission provenance + persistence + replay.

`create_claim(draft)` 把 **Macro Evidence + Company Exposure Evidence → Macro
Transmission Chain → Macro Claim** 的传导分析产物确定性登记为：
Claim + MacroTransmissionChain + MacroTransmissionEvidenceLinks +
ClaimEvidenceLinks，形成
**MacroClaim → MacroTransmissionChain → {Macro Evidence, Company Exposure
Evidence} → (MacroObservation|SourceRecord) → SourceProvider/SourceRecord →
RawArtifact** 的完整可追溯传导链。**0 LLM / 0 DeepSeek / 0 Chroma / 0
Retrieval / 0 LangGraph / 0 Report / 0 Audit**。

**Transmission 不是 EvidenceCard**：传导链是分析产物（利率 → financing channel →
公司有息负债 → 融资成本压力），禁止伪装成来源事实。

流程（两步提交结构，镜像 FinancialClaimService）：
1. **短 DB session** 从真实 PG 加载全部 EvidenceCards 并**逐条校验**：全部存在
   （缺失 → MacroClaimEvidenceNotFound）；company 与 draft 一致（跨公司 →
   MacroClaimEvidenceCompanyMismatch）；按角色校验 origin（macro_driver 必须
   origin_type=macro_observation；company_exposure / observed_effect 必须
   document_chunk；违反 → MacroClaimOriginViolation）；temporal policy（任一已知
   时间晚于 analysis_as_of → MacroClaimFutureEvidence；每个 macro_driver /
   company_exposure 至少一个可用时间，无 → MacroClaimTemporalEvidenceInsufficient，
   **不伪造缺失日期**）；impact-status rule（observed_impact 需 ≥1
   observed_effect，否则 MacroClaimImpactStatusInsufficient——overclaim 防御）；
   critical policy（critical 需 ≥1 macro_driver eligible **且** ≥1 company_exposure
   eligible；observed_impact 时额外 ≥1 observed_effect eligible；否则
   MacroClaimCriticalEvidenceInsufficient；**additional support 不能替代两条传导
   腿**）。随后立即关闭 connection。
2. **纯函数派生**（无 DB）：transmission fingerprint（role-sorted evidence id +
   evidence fingerprint）+ macro claim fingerprint（含 transmission_fingerprint）+
   context expansion（macro_driver / company_exposure / observed_effect 全部
   relation=context——它们单独不能证明"宏观变化导致公司影响"；additional 保持
   supports/contradicts/context）。
3. **单短 PG transaction**：create_or_get Claim（ON CONFLICT(claim_fingerprint)，
   无进程锁）→ create_or_get TransmissionChain（ON CONFLICT(transmission_fingerprint)，
   claim_id = 实际 claim_id）→ bulk insert transmission links + claim evidence
   links。任何 SQLAlchemyError → 整条 rollback + MacroClaimPersistenceFailed（0
   partial write）；无 compensating delete。
4. **Replay**：已有 fingerprint 时重新加载 Claim / MacroTransmissionChain /
   TransmissionEvidenceLinks / EvidenceCards / ClaimEvidenceLinks 并逐项核实
   （company / origin roles / analysis_as_of / temporal / critical / impact-status
   / additional relations / transmission fingerprint / claim fingerprint）；任一损坏
   → MacroClaimIntegrityError，**不自动 repair**。并发 → 最终 1 Claim + 1
   Transmission + 1 套 transmission links + 1 套 ClaimEvidenceLinks。

**不创建 Report / DraftSection / ReviewIssue / Audit**；不接 LangGraph 分析节点；
不改动 historical generic v1 / financial v2-v3 Claims。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.contracts import compute_research_question_sha256
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
    MacroClaimDraft,
    MacroClaimImportance,
    MacroImpactStatus,
    MacroTransmissionRole,
    compute_macro_claim_fingerprint,
    compute_macro_transmission_fingerprint,
)
from app.claims.macro_errors import (
    MacroClaimCriticalEvidenceInsufficient,
    MacroClaimEvidenceCompanyMismatch,
    MacroClaimEvidenceNotFound,
    MacroClaimFutureEvidence,
    MacroClaimImpactStatusInsufficient,
    MacroClaimIntegrityError,
    MacroClaimOriginViolation,
    MacroClaimPersistenceFailed,
    MacroClaimTemporalEvidenceInsufficient,
)
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_transmission_chain import MacroTransmissionChainModel
from app.db.models.macro_transmission_evidence_link import MacroTransmissionEvidenceLinkModel
from app.evidence.contracts import EvidenceOrigin
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_repository import ClaimRepository
from app.repositories.macro_transmission_evidence_link_repository import (
    MacroTransmissionEvidenceLinkRepository,
)
from app.repositories.macro_transmission_repository import MacroTransmissionRepository

_RELATIONS = ("supports", "contradicts", "context")
_TRANSMISSION_ROLES = ("macro_driver", "company_exposure", "observed_effect")


@dataclass(frozen=True)
class MacroClaimResult:
    """一次 create_claim 的结果摘要（不含任何正文文本 / evidence 细节）。"""

    claim_id: UUID
    claim_fingerprint: str
    transmission_id: UUID
    transmission_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _LoadedMacroReferences:
    """加载并校验后的全部 Evidence 引用（真实 PG 数据）。"""

    evidence: dict[UUID, EvidenceCardModel]  # card_id -> card（transmission + additional）
    macro_observations: dict[UUID, MacroObservationModel]  # obs_id -> obs（macro 卡）


@dataclass(frozen=True)
class _DerivedMacroClaim:
    """纯函数阶段派生的全部确定性值（fingerprint / links / 策略结果）。"""

    claim_fingerprint: str
    transmission_fingerprint: str
    question_sha256: str
    evidence_by_relation: dict[str, list[UUID]]  # relation -> sorted evidence ids


class MacroClaimService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_claim(self, draft: MacroClaimDraft) -> MacroClaimResult:
        """登记一条引用 Macro + Company Exposure Evidence 的 Macro Claim（0 partial write）。"""
        # 1. 短 DB session：加载并校验全部 Evidence 引用 / origin / temporal /
        #    impact-status / critical 策略，随后关闭。
        async with self._sessionmaker() as session:
            loaded = await self._load_validate_session(session, draft)

        # 2. 纯函数派生：fingerprints + context expansion（无 DB）。
        derived = self._derive(draft, loaded)

        # 3. 单短 PG transaction：Claim + Transmission + links，原子。
        async with self._sessionmaker() as session:
            try:
                return await self._persist(session, draft, loaded, derived)
            except MacroClaimIntegrityError:
                await session.rollback()
                raise
            except SQLAlchemyError as exc:
                await session.rollback()
                raise MacroClaimPersistenceFailed() from exc

    # ------------------------------------------------------------------ 加载校验

    async def _load_validate_session(
        self,
        session: AsyncSession,
        draft: MacroClaimDraft,
    ) -> _LoadedMacroReferences:
        """加载并校验全部 EvidenceCards + temporal / impact-status / critical 策略（J）。

        - 全部引用卡存在（缺失 → MacroClaimEvidenceNotFound）；
        - 全部卡 company == draft.company_id（跨公司 → MacroClaimEvidenceCompanyMismatch）；
        - 按角色校验 origin：macro_driver 必须 macro_observation；company_exposure /
          observed_effect 必须 document_chunk（违反 → MacroClaimOriginViolation）；
        - temporal：任一已知时间晚于 analysis_as_of → MacroClaimFutureEvidence；每个
          macro_driver / company_exposure 至少一个可用时间（无 →
          MacroClaimTemporalEvidenceInsufficient；不伪造缺失日期）；
        - impact-status：observed_impact 需 ≥1 observed_effect（否则
          MacroClaimImpactStatusInsufficient）；
        - critical：需 eligible 的 macro_driver **且** company_exposure（observed_impact
          时额外 eligible observed_effect；否则 MacroClaimCriticalEvidenceInsufficient）。
        """
        evidence = await self._load_evidence_cards(session, self._all_card_ids(draft))
        macro_observations = await self._load_macro_observations(session, evidence)

        # 公司隔离：全部 Evidence 必须属于 draft 的 company（additional 也不能绕过）。
        await self._check_company(evidence, draft.company_id)

        # 角色 origin 校验（additional 允许任何已存在 origin，但不能绕过公司隔离）。
        for card_id in draft.macro_driver_evidence_ids:
            if evidence[card_id].origin_type != EvidenceOrigin.MACRO_OBSERVATION.value:
                raise MacroClaimOriginViolation(
                    "macro_driver evidence must be origin_type=macro_observation"
                )
        for card_id in draft.company_exposure_evidence_ids + draft.observed_effect_evidence_ids:
            if evidence[card_id].origin_type != EvidenceOrigin.DOCUMENT_CHUNK.value:
                raise MacroClaimOriginViolation(
                    "company_exposure / observed_effect evidence must be origin_type=document_chunk"
                )

        # temporal：先做"已知时间不晚于 analysis_as_of"，再做"每个 macro_driver /
        # company_exposure 至少一个可用时间"。
        for card in evidence.values():
            usable = self._usable_date(card, macro_observations)
            if usable is not None and usable > draft.analysis_as_of:
                raise MacroClaimFutureEvidence()
        for card_id in draft.macro_driver_evidence_ids + draft.company_exposure_evidence_ids:
            if self._usable_date(evidence[card_id], macro_observations) is None:
                raise MacroClaimTemporalEvidenceInsufficient()

        # impact-status rule（overclaim 防御）：observed_impact 需 ≥1 observed_effect。
        if (
            draft.impact_status == MacroImpactStatus.OBSERVED_IMPACT
            and not draft.observed_effect_evidence_ids
        ):
            raise MacroClaimImpactStatusInsufficient()

        # critical policy：critical 需要 eligible 的 macro_driver + company_exposure
        # （observed_impact 时额外 eligible observed_effect）；additional support 不
        # 能替代两条传导腿。
        if draft.importance == MacroClaimImportance.CRITICAL:
            macro_eligible = any(
                evidence[cid].critical_claim_eligible_snapshot
                for cid in draft.macro_driver_evidence_ids
            )
            exposure_eligible = any(
                evidence[cid].critical_claim_eligible_snapshot
                for cid in draft.company_exposure_evidence_ids
            )
            if not (macro_eligible and exposure_eligible):
                raise MacroClaimCriticalEvidenceInsufficient()
            if draft.impact_status == MacroImpactStatus.OBSERVED_IMPACT and not any(
                evidence[cid].critical_claim_eligible_snapshot
                for cid in draft.observed_effect_evidence_ids
            ):
                raise MacroClaimCriticalEvidenceInsufficient()

        return _LoadedMacroReferences(
            evidence=evidence,
            macro_observations=macro_observations,
        )

    @staticmethod
    def _all_card_ids(draft: MacroClaimDraft) -> set[UUID]:
        return (
            set(draft.macro_driver_evidence_ids)
            | set(draft.company_exposure_evidence_ids)
            | set(draft.observed_effect_evidence_ids)
            | set(draft.additional_support_evidence_ids)
            | set(draft.additional_contradict_evidence_ids)
            | set(draft.additional_context_evidence_ids)
        )

    @staticmethod
    async def _load_evidence_cards(
        session: AsyncSession,
        card_ids: set[UUID],
    ) -> dict[UUID, EvidenceCardModel]:
        """从真实 PG 加载 EvidenceCards；缺失 → MacroClaimEvidenceNotFound。

        公司一致性不在此处校验（统一走 _check_company 一步）。
        """
        if not card_ids:
            return {}
        result = await session.execute(
            select(EvidenceCardModel).where(EvidenceCardModel.evidence_card_id.in_(card_ids))
        )
        rows = list(result.scalars().all())
        by_id = {card.evidence_card_id: card for card in rows}
        if len(by_id) != len(card_ids):
            raise MacroClaimEvidenceNotFound()
        return by_id

    async def _load_macro_observations(
        self,
        session: AsyncSession,
        evidence: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, MacroObservationModel]:
        """加载 macro 卡的 MacroObservation（用于可用时间）；缺失 → IntegrityError。

        macro 卡由 ck_evidence_cards_origin_consistency 保证 macro_observation_id
        非空；观测行缺失 = 数据损坏，不自动修复。
        """
        obs_ids = {
            card.macro_observation_id
            for card in evidence.values()
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
        }
        if not obs_ids:
            return {}
        result = await session.execute(
            select(MacroObservationModel).where(MacroObservationModel.observation_id.in_(obs_ids))
        )
        rows = list(result.scalars().all())
        by_id = {row.observation_id: row for row in rows}
        if len(by_id) != len(obs_ids):
            raise MacroClaimIntegrityError(
                "macro claim evidence observation missing (corrupted provenance)"
            )
        return by_id

    @staticmethod
    def _usable_date(
        card: EvidenceCardModel,
        macro_observations: dict[UUID, MacroObservationModel],
    ) -> date | None:
        """Evidence 的可用时间（真实 provenance，不伪造缺失日期）。

        - macro 卡：MacroObservation.normalized_period_start（source_published_at /
          reporting_period_end 恒为 NULL）；
        - document 卡：source_published_at（优先）否则 reporting_period_end。
        """
        if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
            obs = macro_observations.get(card.macro_observation_id)
            if obs is None:
                raise MacroClaimIntegrityError(
                    "macro claim evidence observation missing (corrupted provenance)"
                )
            return obs.normalized_period_start
        if card.source_published_at is not None:
            return card.source_published_at.date()
        return card.reporting_period_end

    # ------------------------------------------------------------------ 公司一致性

    async def _check_company(
        self,
        evidence: dict[UUID, EvidenceCardModel],
        company_id: UUID,
    ) -> None:
        for card in evidence.values():
            if card.company_id != company_id:
                raise MacroClaimEvidenceCompanyMismatch()

    # ------------------------------------------------------------------ 纯函数派生

    @staticmethod
    def _transmission_role_entries(
        card_ids: list[UUID],
        evidence: dict[UUID, EvidenceCardModel],
    ) -> list[dict]:
        """role-sorted evidence_card_id + evidence_fingerprint（真实稳定指纹，不伪造）。"""
        return [
            {
                "evidence_card_id": str(card_id),
                "evidence_fingerprint": evidence[card_id].evidence_fingerprint,
            }
            for card_id in sorted(card_ids, key=str)
        ]

    def _derive(
        self,
        draft: MacroClaimDraft,
        loaded: _LoadedMacroReferences,
    ) -> _DerivedMacroClaim:
        """纯函数派生：transmission fingerprint + claim fingerprint + context expansion。"""
        transmission_fingerprint = compute_macro_transmission_fingerprint(
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
            company_id=draft.company_id,
            channel_type=draft.channel_type.value,
            effect_direction=draft.effect_direction.value,
            impact_status=draft.impact_status.value,
            time_alignment=draft.time_alignment.value,
            analysis_as_of=draft.analysis_as_of,
            macro_driver=self._transmission_role_entries(
                draft.macro_driver_evidence_ids, loaded.evidence
            ),
            company_exposure=self._transmission_role_entries(
                draft.company_exposure_evidence_ids, loaded.evidence
            ),
            observed_effect=self._transmission_role_entries(
                draft.observed_effect_evidence_ids, loaded.evidence
            ),
        )

        # context expansion：macro_driver / company_exposure / observed_effect 全部
        # relation=context（它们单独不能证明"宏观变化导致公司影响"；真实传导语义
        # 由 MacroTransmissionChain + MacroTransmissionEvidenceLinks 承载）。
        transmission_ids = (
            set(draft.macro_driver_evidence_ids)
            | set(draft.company_exposure_evidence_ids)
            | set(draft.observed_effect_evidence_ids)
        )
        evidence_by_relation = {
            "supports": draft.additional_support_evidence_ids,
            "contradicts": draft.additional_contradict_evidence_ids,
            "context": sorted(
                transmission_ids | set(draft.additional_context_evidence_ids),
                key=str,
            ),
        }

        claim_fingerprint = compute_macro_claim_fingerprint(
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION,
            company_id=draft.company_id,
            research_question=draft.research_question,
            analysis_as_of=draft.analysis_as_of,
            statement=draft.statement,
            claim_kind=draft.claim_kind.value,
            confidence=draft.confidence.value,
            importance=draft.importance.value,
            analyst_name=draft.analyst_name,
            analyst_version=draft.analyst_version,
            analyst_model_id=draft.analyst_model_id,
            transmission_fingerprint=transmission_fingerprint,
            additional_supports=draft.additional_support_evidence_ids,
            additional_contradicts=draft.additional_contradict_evidence_ids,
            additional_context=draft.additional_context_evidence_ids,
        )
        return _DerivedMacroClaim(
            claim_fingerprint=claim_fingerprint,
            transmission_fingerprint=transmission_fingerprint,
            question_sha256=compute_research_question_sha256(draft.research_question),
            evidence_by_relation=evidence_by_relation,
        )

    # ------------------------------------------------------------------ 持久化

    async def _persist(
        self,
        session: AsyncSession,
        draft: MacroClaimDraft,
        loaded: _LoadedMacroReferences,
        derived: _DerivedMacroClaim,
    ) -> MacroClaimResult:
        """单 transaction：Claim + MacroTransmissionChain + links，原子（0 partial write）。"""
        claim_repo = ClaimRepository(session)
        chain_repo = MacroTransmissionRepository(session)
        trans_link_repo = MacroTransmissionEvidenceLinkRepository(session)
        ev_link_repo = ClaimEvidenceLinkRepository(session)

        existing = await claim_repo.get_by_fingerprint(derived.claim_fingerprint)
        if existing is not None:
            # Replay：不写任何行，逐项核实后返回既有对象。
            await self._verify_replay(session, existing, draft, derived)
            chain = await chain_repo.get_by_claim_id(existing.claim_id)
            if chain is None:
                raise MacroClaimIntegrityError(
                    "macro claim replay: transmission chain missing for existing claim"
                )
            return MacroClaimResult(
                claim_id=existing.claim_id,
                claim_fingerprint=existing.claim_fingerprint,
                transmission_id=chain.transmission_id,
                transmission_fingerprint=chain.transmission_fingerprint,
                replayed=True,
            )

        claim = ClaimModel(
            claim_id=uuid.uuid4(),
            company_id=draft.company_id,
            research_question=draft.research_question,
            research_question_sha256=derived.question_sha256,
            statement=draft.statement,
            analysis_domain="macro",
            claim_kind=draft.claim_kind.value,
            confidence=draft.confidence.value,
            importance=draft.importance.value,
            analyst_name=draft.analyst_name,
            analyst_version=draft.analyst_version,
            analyst_model_id=draft.analyst_model_id,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION,
            claim_fingerprint=derived.claim_fingerprint,
        )
        persisted_claim, claim_created = await claim_repo.create_or_get(claim)
        if not claim_created:
            # 并发输家：复用既有 Claim（replay 校验后返回，无任何写）。
            await self._verify_replay(session, persisted_claim, draft, derived)
            chain = await chain_repo.get_by_claim_id(persisted_claim.claim_id)
            if chain is None:
                raise MacroClaimIntegrityError(
                    "macro claim replay: transmission chain missing for existing claim"
                )
            return MacroClaimResult(
                claim_id=persisted_claim.claim_id,
                claim_fingerprint=persisted_claim.claim_fingerprint,
                transmission_id=chain.transmission_id,
                transmission_fingerprint=chain.transmission_fingerprint,
                replayed=True,
            )

        # 本 transaction 创建了 Claim：创建对应的 Transmission + links（同一事务内
        # 原子；任一失败 → 整条 rollback，0 partial write）。
        chain = MacroTransmissionChainModel(
            transmission_id=uuid.uuid4(),
            claim_id=persisted_claim.claim_id,
            company_id=draft.company_id,
            channel_type=draft.channel_type.value,
            effect_direction=draft.effect_direction.value,
            impact_status=draft.impact_status.value,
            time_alignment=draft.time_alignment.value,
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
            transmission_fingerprint=derived.transmission_fingerprint,
        )
        persisted_chain, chain_created = await chain_repo.create_or_get(chain)
        if not chain_created:
            # 本应不可能：Claim 是本事务新创建（唯一 fingerprint），同 draft 派生出的
            # transmission fingerprint 必然也是新的。出现复用 → 数据损坏。
            raise MacroClaimIntegrityError(
                "macro claim transmission fingerprint conflict on freshly created claim"
            )
        await trans_link_repo.bulk_insert(
            self._transmission_links(persisted_chain.transmission_id, draft)
        )
        await ev_link_repo.bulk_insert(self._evidence_links(persisted_claim.claim_id, derived))
        await session.commit()
        return MacroClaimResult(
            claim_id=persisted_claim.claim_id,
            claim_fingerprint=persisted_claim.claim_fingerprint,
            transmission_id=persisted_chain.transmission_id,
            transmission_fingerprint=persisted_chain.transmission_fingerprint,
            replayed=False,
        )

    @staticmethod
    def _transmission_links(
        transmission_id: UUID,
        draft: MacroClaimDraft,
    ) -> list[MacroTransmissionEvidenceLinkModel]:
        links: list[MacroTransmissionEvidenceLinkModel] = []
        for role, card_ids in (
            (MacroTransmissionRole.MACRO_DRIVER.value, draft.macro_driver_evidence_ids),
            (MacroTransmissionRole.COMPANY_EXPOSURE.value, draft.company_exposure_evidence_ids),
            (MacroTransmissionRole.OBSERVED_EFFECT.value, draft.observed_effect_evidence_ids),
        ):
            for card_id in card_ids:
                links.append(
                    MacroTransmissionEvidenceLinkModel(
                        transmission_id=transmission_id,
                        evidence_card_id=card_id,
                        role=role,
                    )
                )
        return links

    @staticmethod
    def _evidence_links(
        claim_id: UUID,
        derived: _DerivedMacroClaim,
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

    # ------------------------------------------------------------------ replay

    async def _verify_replay(
        self,
        session: AsyncSession,
        existing: ClaimModel,
        draft: MacroClaimDraft,
        derived: _DerivedMacroClaim,
    ) -> None:
        """已有 fingerprint 的 Macro Claim replay 完整性校验（M）。

        重新加载全部 Evidence + MacroObservations，重新执行 origin / temporal /
        impact-status / critical 策略与派生，逐项核实 Claim 字段、claim evidence
        links、MacroTransmissionChain 字段、transmission links 与 transmission
        fingerprint。任一损坏 → MacroClaimIntegrityError，**不自动 repair**。
        """
        loaded = await self._load_validate_session(session, draft)
        rederived = self._derive(draft, loaded)
        if rederived.claim_fingerprint != derived.claim_fingerprint:
            raise MacroClaimIntegrityError(
                "macro claim replay integrity check failed on derived fingerprint"
            )

        pairs = (
            ("company_id", existing.company_id, draft.company_id),
            ("research_question", existing.research_question, draft.research_question),
            (
                "research_question_sha256",
                existing.research_question_sha256,
                rederived.question_sha256,
            ),
            ("statement", existing.statement, draft.statement),
            ("analysis_domain", existing.analysis_domain, "macro"),
            ("claim_kind", existing.claim_kind, draft.claim_kind.value),
            ("confidence", existing.confidence, draft.confidence.value),
            ("importance", existing.importance, draft.importance.value),
            ("analyst_name", existing.analyst_name, draft.analyst_name),
            ("analyst_version", existing.analyst_version, draft.analyst_version),
            ("analyst_model_id", existing.analyst_model_id, draft.analyst_model_id),
            ("claim_schema_version", existing.claim_schema_version, MACRO_CLAIM_SCHEMA_VERSION),
            ("claim_fingerprint", existing.claim_fingerprint, rederived.claim_fingerprint),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise MacroClaimIntegrityError(
                    f"macro claim replay integrity check failed on {name}"
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
            if actual_ev[relation] != rederived.evidence_by_relation[relation]:
                raise MacroClaimIntegrityError(
                    f"macro claim replay integrity check failed on links[{relation}]"
                )

        chain_repo = MacroTransmissionRepository(session)
        chain = await chain_repo.get_by_claim_id(existing.claim_id)
        if chain is None:
            raise MacroClaimIntegrityError(
                "macro claim replay: transmission chain missing for existing claim"
            )
        chain_pairs = (
            ("company_id", chain.company_id, draft.company_id),
            ("channel_type", chain.channel_type, draft.channel_type.value),
            ("effect_direction", chain.effect_direction, draft.effect_direction.value),
            ("impact_status", chain.impact_status, draft.impact_status.value),
            ("time_alignment", chain.time_alignment, draft.time_alignment.value),
            (
                "transmission_schema_version",
                chain.transmission_schema_version,
                MACRO_TRANSMISSION_SCHEMA_VERSION,
            ),
            (
                "transmission_fingerprint",
                chain.transmission_fingerprint,
                rederived.transmission_fingerprint,
            ),
        )
        for name, stored, expected in chain_pairs:
            if stored != expected:
                raise MacroClaimIntegrityError(
                    f"macro claim replay integrity check failed on transmission[{name}]"
                )

        trans_links = await MacroTransmissionEvidenceLinkRepository(session).list_by_transmission(
            chain.transmission_id
        )
        actual_by_role = {
            role: sorted(
                (link.evidence_card_id for link in trans_links if link.role == role),
                key=str,
            )
            for role in _TRANSMISSION_ROLES
        }
        expected_by_role = {
            MacroTransmissionRole.MACRO_DRIVER.value: sorted(
                draft.macro_driver_evidence_ids, key=str
            ),
            MacroTransmissionRole.COMPANY_EXPOSURE.value: sorted(
                draft.company_exposure_evidence_ids, key=str
            ),
            MacroTransmissionRole.OBSERVED_EFFECT.value: sorted(
                draft.observed_effect_evidence_ids, key=str
            ),
        }
        for role in _TRANSMISSION_ROLES:
            if actual_by_role[role] != expected_by_role[role]:
                raise MacroClaimIntegrityError(
                    f"macro claim replay integrity check failed on transmission links[{role}]"
                )
