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
1. **短 DB session** 从真实 PG 加载全部 EvidenceCards 并**逐条校验**（v6/v3 当前
   规则，legacy v5/v2 与 v4/v1 走历史规则）：全部存在（缺失 →
   MacroClaimEvidenceNotFound）；
   company 与 draft 一致（跨公司 → MacroClaimEvidenceCompanyMismatch）；按角色
   校验 origin（macro_driver 允许 macro_observation，或经过明确筛选的 external
   event document Evidence——news_article + evidence_type ∈ {event, fact,
   statement}；company_exposure / observed_effect 必须 document_chunk；违反 →
   MacroClaimOriginViolation）；information availability（**全部**进入 Claim 的
   Evidence——transmission roles + additional——availability <= analysis_as_of；
   未来 → MacroClaimFutureEvidence；无法解析 → MacroClaimTemporalEvidenceInsufficient，
   **不伪造缺失日期**。document 用 SourceRecord.published_at，否则
   acquired_at，**绝不用 reporting_period_end**；macro 用 snapshot.fetched_at，
   **绝不用 normalized_period_start**）；impact-status rule（observed_impact 需
   ≥1 observed_effect，否则 MacroClaimImpactStatusInsufficient）；time-alignment
   policy（observed_impact 必须 aligned；uncertain 只允许 plausible + risk +
   normal；违反 → MacroClaimTimeAlignmentPolicy）；critical policy（critical 需
   ≥1 macro_driver eligible **且** ≥1 company_exposure eligible，并要求
   time_alignment=aligned **且** effect_direction != uncertain；observed_impact
   时额外 ≥1 observed_effect eligible；否则 MacroClaimCriticalEvidenceInsufficient；
   **additional support 不能替代两条传导腿**）。随后立即关闭 connection。
2. **纯函数派生**（无 DB）：transmission fingerprint（role-sorted evidence id +
   evidence fingerprint，schema_version=3）+ macro claim fingerprint（含
   transmission_fingerprint，schema_version=6）+ context expansion（macro_driver /
   company_exposure / observed_effect 全部 relation=context——它们单独不能证明
   "宏观变化导致公司影响"；additional 保持 supports/contradicts/context）。
3. **单短 PG transaction**：create_or_get Claim（ON CONFLICT(claim_fingerprint)，
   无进程锁）→ **plain INSERT** TransmissionChain（transmission_fingerprint **不是
   global identity**，无 ON CONFLICT；claim_id 是新生成且 UNIQUE，无冲突；新链
   **persist analysis_as_of=draft.analysis_as_of**——v3 查询列，CHECK
   `transmission_schema_version < 3 OR analysis_as_of IS NOT NULL` 兜底）→ bulk
   insert transmission links + claim evidence links。任何 SQLAlchemyError → 整条
   rollback + MacroClaimPersistenceFailed（0 partial write）；无 compensating
   delete。`create_claim_batch` 为 **all-drafts-validate-first + 单 transaction**
   （任一 draft 校验失败 → 整批拒绝，0 写；items 按 input drafts 顺序返回）。
4. **Replay**（**three-tier，version-aware**）：已有 fingerprint 时按既有 Claim
   的 claim_schema_version 分叉——v6 → 当前 v3/v6 规则（**额外核验
   chain.analysis_as_of == draft.analysis_as_of**）、v5 → v2/v5 历史规则
   （0024-era，chain.analysis_as_of=NULL 允许，不反推）、v4 → v1/v4 历史规则
   （不把旧历史对象误判损坏）；重新加载 Claim / MacroTransmissionChain /
   TransmissionEvidenceLinks / EvidenceCards / ClaimEvidenceLinks 并逐项核实
   （company / origin roles / availability / temporal / critical / impact-status /
   time-alignment / additional relations / transmission fingerprint / claim
   fingerprint）；任一损坏 → MacroClaimIntegrityError，**不自动 repair**。并发 →
   最终 1 Claim + 1 Transmission + 1 套 transmission links + 1 套
   ClaimEvidenceLinks。

**不创建 Report / DraftSection / ReviewIssue / Audit**；不接 LangGraph 分析节点；
不改动 historical generic v1 / financial v2-v3 Claims。
"""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.claims.contracts import ClaimKind, compute_research_question_sha256
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_CLAIM_SCHEMA_VERSION_V4,
    MACRO_CLAIM_SCHEMA_VERSION_V5,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION_V1,
    MACRO_TRANSMISSION_SCHEMA_VERSION_V2,
    MacroClaimDraft,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
    MacroTransmissionRole,
    compute_macro_claim_fingerprint,
    compute_macro_transmission_fingerprint,
)
from app.claims.macro_errors import (
    MacroClaimCriticalEvidenceInsufficient,
    MacroClaimDraftError,
    MacroClaimEvidenceCompanyMismatch,
    MacroClaimEvidenceNotFound,
    MacroClaimFutureEvidence,
    MacroClaimImpactStatusInsufficient,
    MacroClaimIntegrityError,
    MacroClaimOriginViolation,
    MacroClaimPersistenceFailed,
    MacroClaimTemporalEvidenceInsufficient,
    MacroClaimTimeAlignmentPolicy,
)
from app.claims.macro_policy import driver_evidence_eligible, resolve_availability
from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_transmission_chain import MacroTransmissionChainModel
from app.db.models.macro_transmission_evidence_link import MacroTransmissionEvidenceLinkModel
from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import EvidenceOrigin
from app.repositories.claim_evidence_link_repository import ClaimEvidenceLinkRepository
from app.repositories.claim_repository import ClaimRepository
from app.repositories.macro_transmission_evidence_link_repository import (
    MacroTransmissionEvidenceLinkRepository,
)
from app.repositories.macro_transmission_repository import MacroTransmissionRepository

_RELATIONS = ("supports", "contradicts", "context")
_TRANSMISSION_ROLES = ("macro_driver", "company_exposure", "observed_effect")

# 单次 create_claim_batch 最多 3 条 Macro Claim（与 4C.1B 的
# MacroAnalysisDecision MAX_CLAIMS_PER_DECISION 一致）。
MAX_MACRO_CLAIMS_PER_BATCH = 3


@dataclass(frozen=True)
class MacroClaimResult:
    """一次 create_claim 的结果摘要（不含任何正文文本 / evidence 细节）。"""

    claim_id: UUID
    claim_fingerprint: str
    transmission_id: UUID
    transmission_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class MacroClaimBatchItem:
    """batch 中单个 draft 的结果（ordinal 从 1 开始，与 input drafts 一一对应）。

    - ordinal：draft 在本次 batch 中的位置（1..len(drafts)）；
    - claim_id / transmission_id：created 或 replayed 后的 Claim / 链 id；
    - replayed：True=复用既有 fingerprint 的 Claim，False=本次真正新增。
    """

    ordinal: int
    claim_id: UUID
    transmission_id: UUID
    replayed: bool


@dataclass(frozen=True)
class MacroClaimBatchResult:
    """一次 create_claim_batch 的结果摘要（不含任何正文文本 / evidence）。

    - items：**ordered result**——按 input drafts 顺序的逐条结果，
      len(items) == len(drafts)，items[i] 永远对应 drafts[i]（不按
      created/replayed 分组重排）；
    - fingerprints：claim_id → claim_fingerprint；transmission_fingerprints：
      claim_id → transmission_fingerprint（供上游追溯）；
    - claim_ids / created / replayed / created_count / replayed_count：由
      items 顺序派生（不是各自分组拼接）。
    """

    items: tuple[MacroClaimBatchItem, ...]
    fingerprints: dict[UUID, str]
    transmission_fingerprints: dict[UUID, str]

    @property
    def claim_ids(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items)

    @property
    def created(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items if not item.replayed)

    @property
    def replayed(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items if item.replayed)

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def replayed_count(self) -> int:
        return len(self.replayed)


@dataclass(frozen=True)
class _LoadedMacroReferences:
    """加载并校验后的全部 Evidence 引用（真实 PG 数据）。"""

    evidence: dict[UUID, EvidenceCardModel]  # card_id -> card（transmission + additional）
    macro_observations: dict[UUID, MacroObservationModel]  # obs_id -> obs（legacy 可用时间）
    macro_snapshots: dict[UUID, MacroDatasetSnapshotModel]  # snapshot_id -> snapshot
    source_records: dict[UUID, SourceRecordModel]  # source_id -> source


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
        batch = await self.create_claim_batch([draft])
        item = batch.items[0]
        return MacroClaimResult(
            claim_id=item.claim_id,
            claim_fingerprint=batch.fingerprints[item.claim_id],
            transmission_id=item.transmission_id,
            transmission_fingerprint=batch.transmission_fingerprints[item.claim_id],
            replayed=item.replayed,
        )

    async def create_claim_batch(
        self, drafts: list[MacroClaimDraft]
    ) -> MacroClaimBatchResult:
        """把 1..MAX_MACRO_CLAIMS_PER_BATCH 条 Macro Claim 原子登记。

        两步提交结构（镜像 FinancialClaimService.create_claim_batch）：
        1. **all-drafts-validate-first**——开事务前，对全部 drafts 加载引用并完成
           派生；任何一条失败 → 整批拒绝（0 写），**不允许 candidate 1 创建、
           candidate 2 才失败**。
        2. **单 transaction**——逐个 create_or_get Claim + plain INSERT
           TransmissionChain（新链 persist analysis_as_of）+ bulk insert links /
           replay 校验；任一 SQLAlchemyError / MacroClaimIntegrityError → 整批
           回滚，不留下半批 Claim（禁止 compensating delete）。
        items 按 input drafts 顺序返回（ordinal 一一对应）。
        """
        if not isinstance(drafts, list) or not (1 <= len(drafts) <= MAX_MACRO_CLAIMS_PER_BATCH):
            raise MacroClaimDraftError(f"drafts 必须在 1..{MAX_MACRO_CLAIMS_PER_BATCH} 条")

        # 1. 短 DB session：一次性加载并校验全部 drafts 的 Evidence 引用。
        async with self._sessionmaker() as session:
            loaded_list = [await self._load_validate_session(session, draft) for draft in drafts]

        # 2. 全部 drafts 先完成派生（任何一条失败 → 整批拒绝，0 写）。新建 Claim 恒为
        #    当前 schema（v6/v3）；版本不同 → fingerprint 不同 → 不与历史 v5/v2、v4/v1 冲突。
        derived_list = [
            self._derive(
                draft,
                loaded,
                claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION,
                transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
            )
            for draft, loaded in zip(drafts, loaded_list, strict=True)
        ]

        # 3. 单 transaction：逐个 create_or_get + chain + links / replay。
        #    items 按 prepared（== input drafts）顺序收集，绝不按 created/replayed
        #    分组重排——items[i] 永远对应 drafts[i]。
        fingerprints: dict[UUID, str] = {}
        transmission_fingerprints: dict[UUID, str] = {}
        items: list[MacroClaimBatchItem] = []
        async with self._sessionmaker() as session:
            try:
                for ordinal, (draft, derived) in enumerate(
                    zip(drafts, derived_list, strict=True), start=1
                ):
                    claim_id, transmission_id, replayed = await self._persist_one(
                        session, draft, loaded_list[ordinal - 1], derived
                    )
                    fingerprints[claim_id] = derived.claim_fingerprint
                    transmission_fingerprints[claim_id] = derived.transmission_fingerprint
                    items.append(
                        MacroClaimBatchItem(
                            ordinal=ordinal,
                            claim_id=claim_id,
                            transmission_id=transmission_id,
                            replayed=replayed,
                        )
                    )
                await session.commit()
                return MacroClaimBatchResult(
                    items=tuple(items),
                    fingerprints=fingerprints,
                    transmission_fingerprints=transmission_fingerprints,
                )
            except MacroClaimIntegrityError:
                # replay 校验发现既有 Claim 数据损坏 → 显式回滚本事务，然后抛出。
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
        *,
        legacy: bool = False,
    ) -> _LoadedMacroReferences:
        """加载并校验全部 EvidenceCards（J；`legacy=True` 走 v1/v4 历史规则）。

        v2/v5 当前规则：
        - 全部引用卡存在（缺失 → MacroClaimEvidenceNotFound）；全部卡
          company == draft.company_id（跨公司 → MacroClaimEvidenceCompanyMismatch）；
        - 角色 origin v2：macro_driver 允许 macro_observation，或经过明确筛选的
          external event document Evidence（SourceRecord.document_type=news_article
          **且** evidence_type ∈ {event, fact, statement}）；company_exposure /
          observed_effect 必须 document_chunk（违反 → MacroClaimOriginViolation）；
        - information availability（no-lookahead）：**全部**进入 Claim 的 Evidence
          （transmission roles + additional）availability <= analysis_as_of；
          未来 → MacroClaimFutureEvidence；无法解析 → MacroClaimTemporalEvidenceInsufficient
          （**不伪造缺失日期**）。document 用 SourceRecord.published_at 否则
          acquired_at（**绝不用 reporting_period_end**）；macro 用 snapshot.fetched_at
          （**绝不用 normalized_period_start**）；
        - impact-status：observed_impact 需 ≥1 observed_effect（否则
          MacroClaimImpactStatusInsufficient）；
        - time-alignment policy：observed_impact 必须 time_alignment=aligned；
          uncertain 只允许 plausible + risk + normal（违反 → MacroClaimTimeAlignmentPolicy）；
        - critical：需 eligible 的 macro_driver **且** company_exposure，并要求
          time_alignment=aligned **且** effect_direction != uncertain（否则
          MacroClaimCriticalEvidenceInsufficient；observed_impact 时额外 eligible
          observed_effect；**additional support 不能替代两条传导腿**）。

        legacy v1/v4：macro_driver 必须 macro_observation；可用时间用
        normalized_period_start / source_published_at / reporting_period_end；不做
        v2 的 document driver 与 time-alignment policy（防止把旧历史对象误判损坏）。
        """
        evidence = await self._load_evidence_cards(session, self._all_card_ids(draft))
        macro_observations = await self._load_macro_observations(session, evidence)

        # 公司隔离：全部 Evidence 必须属于 draft 的 company（additional 也不能绕过）。
        await self._check_company(evidence, draft.company_id)

        if legacy:
            return await self._load_validate_legacy(session, draft, evidence, macro_observations)

        snapshots = await self._load_macro_snapshots(session, evidence)
        source_records = await self._load_source_records(session, evidence)

        # 角色 origin 校验 v2/v3（additional 允许任何已存在 origin，但不能绕过公司隔离）。
        # driver 资格复用 macro_policy.driver_evidence_eligible——MacroAnalysisService
        # 与 MacroClaimService **共用**同一 no-lookahead 策略，禁止重复实现。
        for card_id in draft.macro_driver_evidence_ids:
            card = evidence[card_id]
            source_document_type = None
            if card.origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value:
                source = source_records.get(card.source_id)
                if source is None:
                    raise MacroClaimIntegrityError(
                        "macro claim evidence source missing (corrupted provenance)"
                    )
                source_document_type = source.document_type
            if not driver_evidence_eligible(
                origin_type=card.origin_type,
                evidence_type=card.evidence_type,
                source_document_type=source_document_type,
            ):
                raise MacroClaimOriginViolation(
                    "macro_driver evidence must be macro_observation or an eligible "
                    "external event document (news_article + event/fact/statement)"
                )
        for card_id in draft.company_exposure_evidence_ids + draft.observed_effect_evidence_ids:
            if evidence[card_id].origin_type != EvidenceOrigin.DOCUMENT_CHUNK.value:
                raise MacroClaimOriginViolation(
                    "company_exposure / observed_effect evidence must be origin_type=document_chunk"
                )

        # information availability（no-lookahead）：全部进入 Claim 的 Evidence 都
        # 必须 availability <= analysis_as_of（additional 也不能未来穿越）。
        for card in evidence.values():
            availability = self._availability_at(card, snapshots, source_records)
            if availability is None:
                raise MacroClaimTemporalEvidenceInsufficient()
            if self._normalize_availability(availability).date() > draft.analysis_as_of:
                raise MacroClaimFutureEvidence()

        # impact-status rule（overclaim 防御）：observed_impact 需 ≥1 observed_effect。
        if (
            draft.impact_status == MacroImpactStatus.OBSERVED_IMPACT
            and not draft.observed_effect_evidence_ids
        ):
            raise MacroClaimImpactStatusInsufficient()

        # time-alignment policy v2（确定性一致性，不自动猜 lag）。
        if (
            draft.impact_status == MacroImpactStatus.OBSERVED_IMPACT
            and draft.time_alignment != MacroTimeAlignment.ALIGNED
        ):
            raise MacroClaimTimeAlignmentPolicy("observed_impact requires time_alignment=aligned")
        if draft.time_alignment == MacroTimeAlignment.UNCERTAIN and (
            draft.impact_status != MacroImpactStatus.PLAUSIBLE_IMPACT
            or draft.claim_kind != ClaimKind.RISK
            or draft.importance != MacroClaimImportance.NORMAL
        ):
            raise MacroClaimTimeAlignmentPolicy(
                "time_alignment=uncertain only allows plausible_impact + risk + normal"
            )

        # critical policy v2：critical 需 eligible 双腿 + aligned + 已知方向。
        if draft.importance == MacroClaimImportance.CRITICAL:
            if (
                draft.time_alignment != MacroTimeAlignment.ALIGNED
                or draft.effect_direction == MacroEffectDirection.UNCERTAIN
            ):
                raise MacroClaimCriticalEvidenceInsufficient()
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
            macro_snapshots=snapshots,
            source_records=source_records,
        )

    async def _load_validate_legacy(
        self,
        session: AsyncSession,
        draft: MacroClaimDraft,
        evidence: dict[UUID, EvidenceCardModel],
        macro_observations: dict[UUID, MacroObservationModel],
    ) -> _LoadedMacroReferences:
        """v1/v4 历史 replay 校验（不把旧历史对象误判损坏）。

        保留 4C.1A foundation 的规则：macro_driver 必须 macro_observation；
        可用时间 = macro 用 Observation.normalized_period_start、document 用
        source_published_at / reporting_period_end；不做 v2 的 document driver
        资格与 time-alignment policy。
        """
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

        for card in evidence.values():
            usable = self._legacy_usable_date(card, macro_observations)
            if usable is not None and usable > draft.analysis_as_of:
                raise MacroClaimFutureEvidence()
        for card_id in draft.macro_driver_evidence_ids + draft.company_exposure_evidence_ids:
            if self._legacy_usable_date(evidence[card_id], macro_observations) is None:
                raise MacroClaimTemporalEvidenceInsufficient()

        if (
            draft.impact_status == MacroImpactStatus.OBSERVED_IMPACT
            and not draft.observed_effect_evidence_ids
        ):
            raise MacroClaimImpactStatusInsufficient()

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
            macro_snapshots={},
            source_records={},
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

    async def _load_macro_snapshots(
        self,
        session: AsyncSession,
        evidence: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, MacroDatasetSnapshotModel]:
        """加载 macro 卡的 MacroDatasetSnapshot（v2 availability = snapshot.fetched_at）。

        macro 卡由 ck_evidence_cards_origin_consistency 保证 macro_snapshot_id
        非空；快照行缺失 = 数据损坏，不自动修复。
        """
        snapshot_ids = {
            card.macro_snapshot_id
            for card in evidence.values()
            if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value
            and card.macro_snapshot_id is not None
        }
        if not snapshot_ids:
            return {}
        result = await session.execute(
            select(MacroDatasetSnapshotModel).where(
                MacroDatasetSnapshotModel.snapshot_id.in_(snapshot_ids)
            )
        )
        rows = list(result.scalars().all())
        by_id = {row.snapshot_id: row for row in rows}
        if len(by_id) != len(snapshot_ids):
            raise MacroClaimIntegrityError(
                "macro claim evidence snapshot missing (corrupted provenance)"
            )
        return by_id

    async def _load_source_records(
        self,
        session: AsyncSession,
        evidence: dict[UUID, EvidenceCardModel],
    ) -> dict[UUID, SourceRecordModel]:
        """加载 document 卡的 SourceRecord（v2 availability + document_type 校验）。

        document 卡由 ck_evidence_cards_origin_consistency 保证 source_id 非空；
        来源行缺失 = 数据损坏，不自动修复。
        """
        source_ids = {
            card.source_id
            for card in evidence.values()
            if card.origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value
            and card.source_id is not None
        }
        if not source_ids:
            return {}
        result = await session.execute(
            select(SourceRecordModel).where(SourceRecordModel.source_id.in_(source_ids))
        )
        rows = list(result.scalars().all())
        by_id = {row.source_id: row for row in rows}
        if len(by_id) != len(source_ids):
            raise MacroClaimIntegrityError(
                "macro claim evidence source missing (corrupted provenance)"
            )
        return by_id

    @staticmethod
    def _normalize_availability(dt: datetime) -> datetime:
        """availability normalize 为 UTC aware datetime（day-granularity cutoff）。"""
        if dt.tzinfo is None:
            return dt.replace(tzinfo=UTC)
        return dt.astimezone(UTC)

    def _availability_at(
        self,
        card: EvidenceCardModel,
        macro_snapshots: dict[UUID, MacroDatasetSnapshotModel],
        source_records: dict[UUID, SourceRecordModel],
    ) -> datetime | None:
        """v2/v3 information availability（真实 provenance，不伪造缺失日期）。

        provenance 值解析委托 macro_policy.resolve_availability——MacroAnalysisService
        与 MacroClaimService **共用**同一 no-lookahead 策略，禁止重复实现；
        本方法只负责把缺失 provenance 映射为数据损坏（IntegrityError）。
        """
        if card.origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
            snapshot = macro_snapshots.get(card.macro_snapshot_id)
            if snapshot is None:
                raise MacroClaimIntegrityError(
                    "macro claim evidence snapshot missing (corrupted provenance)"
                )
            return resolve_availability(
                origin_type=card.origin_type,
                snapshot_fetched_at=snapshot.fetched_at,
                source_published_at=None,
                source_acquired_at=None,
            )
        source = source_records.get(card.source_id)
        if source is None:
            raise MacroClaimIntegrityError(
                "macro claim evidence source missing (corrupted provenance)"
            )
        return resolve_availability(
            origin_type=card.origin_type,
            snapshot_fetched_at=None,
            source_published_at=source.published_at,
            source_acquired_at=source.acquired_at,
        )

    @staticmethod
    def _legacy_usable_date(
        card: EvidenceCardModel,
        macro_observations: dict[UUID, MacroObservationModel],
    ) -> date | None:
        """v1/v4 历史可用时间（仅 legacy replay 使用，不伪造缺失日期）。

        - macro 卡：MacroObservation.normalized_period_start；
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
        *,
        claim_schema_version: int,
        transmission_schema_version: int,
    ) -> _DerivedMacroClaim:
        """纯函数派生：transmission fingerprint + claim fingerprint + context expansion。

        schema 版本由调用方决定（新建 = 当前 v2/v5；legacy replay = v1/v4），
        fingerprint 必须包含对应 schema version（版本变化 → 新指纹 → 新对象）。
        """
        transmission_fingerprint = compute_macro_transmission_fingerprint(
            transmission_schema_version=transmission_schema_version,
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
            claim_schema_version=claim_schema_version,
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

    async def _persist_one(
        self,
        session: AsyncSession,
        draft: MacroClaimDraft,
        loaded: _LoadedMacroReferences,
        derived: _DerivedMacroClaim,
    ) -> tuple[UUID, UUID, bool]:
        """事务内持久化一条 Macro Claim；返回 (claim_id, transmission_id, replayed)。

        **不 commit**（batch 由 create_claim_batch 统一 commit）；任何失败抛错由
        调用方回滚。本方法不做 compensating delete。
        """
        claim_repo = ClaimRepository(session)
        chain_repo = MacroTransmissionRepository(session)
        trans_link_repo = MacroTransmissionEvidenceLinkRepository(session)
        ev_link_repo = ClaimEvidenceLinkRepository(session)

        existing = await claim_repo.get_by_fingerprint(derived.claim_fingerprint)
        if existing is not None:
            # Replay：不写任何行，逐项核实后返回既有对象。
            await self._verify_replay(session, existing, draft)
            chain = await chain_repo.get_by_claim_id(existing.claim_id)
            if chain is None:
                raise MacroClaimIntegrityError(
                    "macro claim replay: transmission chain missing for existing claim"
                )
            return existing.claim_id, chain.transmission_id, True

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
            await self._verify_replay(session, persisted_claim, draft)
            chain = await chain_repo.get_by_claim_id(persisted_claim.claim_id)
            if chain is None:
                raise MacroClaimIntegrityError(
                    "macro claim replay: transmission chain missing for existing claim"
                )
            return persisted_claim.claim_id, chain.transmission_id, True

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
            # v3 查询列（Gate 0）：新链必须持久化 analysis_as_of=draft.analysis_as_of，
            # 使 DB 能从 claim_id 反推 cutoff；CHECK
            # `transmission_schema_version < 3 OR analysis_as_of IS NOT NULL` 兜底。
            analysis_as_of=draft.analysis_as_of,
        )
        # transmission_fingerprint **不是 global identity**（0024 移除 UNIQUE），
        # 因此 plain INSERT 即可：claim_id 是本事务新生成的 UNIQUE 值，无冲突可能。
        persisted_chain = await chain_repo.create(chain)
        await trans_link_repo.bulk_insert(
            self._transmission_links(persisted_chain.transmission_id, draft)
        )
        await ev_link_repo.bulk_insert(self._evidence_links(persisted_claim.claim_id, derived))
        return persisted_claim.claim_id, persisted_chain.transmission_id, False

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
    ) -> None:
        """已有 fingerprint 的 Macro Claim replay 完整性校验（three-tier，version-aware）。

        按既有 Claim 的 claim_schema_version 分叉（**不得把历史版本误判损坏**）：
        - v6（当前）：v3/v6 规则（document driver 资格 / availability /
          time-alignment policy），**额外核验 chain.analysis_as_of ==
          draft.analysis_as_of**（0025 起的 v3 查询列语义）；
        - v5（0024-era）：v2/v5 历史规则——与 v6 同一套资格/可用性政策，但 0025
          **不 backfill**，历史链 analysis_as_of=NULL 允许（**不反推 cutoff**）；
        - v4（最旧 legacy）：v1/v4 历史规则（macro_driver 必须 macro_observation；
          normalized_period_start / source_published_at / reporting_period_end 可用
          时间）。

        重新加载全部 Evidence + Observations/Snapshots/SourceRecords，重新执行
        origin / availability / temporal / impact-status / time-alignment / critical
        策略与派生，逐项核实 Claim 字段、claim evidence links、MacroTransmissionChain
        字段、transmission links 与 transmission fingerprint。任一损坏 →
        MacroClaimIntegrityError，**不自动 repair**。
        """
        if existing.claim_schema_version == MACRO_CLAIM_SCHEMA_VERSION:
            legacy = False
            claim_version = MACRO_CLAIM_SCHEMA_VERSION
            trans_version = MACRO_TRANSMISSION_SCHEMA_VERSION
            verify_cutoff = True
        elif existing.claim_schema_version == MACRO_CLAIM_SCHEMA_VERSION_V5:
            legacy = False
            claim_version = MACRO_CLAIM_SCHEMA_VERSION_V5
            trans_version = MACRO_TRANSMISSION_SCHEMA_VERSION_V2
            verify_cutoff = False
        elif existing.claim_schema_version == MACRO_CLAIM_SCHEMA_VERSION_V4:
            legacy = True
            claim_version = MACRO_CLAIM_SCHEMA_VERSION_V4
            trans_version = MACRO_TRANSMISSION_SCHEMA_VERSION_V1
            verify_cutoff = False
        else:
            raise MacroClaimIntegrityError("macro claim replay: unknown claim_schema_version")

        loaded = await self._load_validate_session(session, draft, legacy=legacy)
        rederived = self._derive(
            draft,
            loaded,
            claim_schema_version=claim_version,
            transmission_schema_version=trans_version,
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
            ("claim_schema_version", existing.claim_schema_version, claim_version),
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
                trans_version,
            ),
            (
                "transmission_fingerprint",
                chain.transmission_fingerprint,
                rederived.transmission_fingerprint,
            ),
        )
        if verify_cutoff:
            # v6 当前层：v3 链必须持久化 analysis_as_of（0025 查询列）；历史 v5/v4
            # 链允许 NULL，**不参与该检查（不反推 cutoff）**。
            chain_pairs = chain_pairs + (
                ("analysis_as_of", chain.analysis_as_of, draft.analysis_as_of),
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
