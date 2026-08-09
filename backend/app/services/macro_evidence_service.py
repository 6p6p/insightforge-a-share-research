"""Macro evidence card service (stage 3C.3A): deterministic macro provenance + persistence.

create_macro_card(draft) 把"已确认与研究问题相关的宏观测语义输入"确定性登记为
一张 macro_observation origin 的 EvidenceCard。**无 LLM、无 Chroma、无
DocumentChunk、无 quote resolver**：调用方显式提交 MacroEvidenceDraft，
Service 只做确定性派生与幂等落库，绝不重新解释数值。

流程：
1. 短 DB session 读真实 macro provenance：Company（由调用方当前研究上下文
   提供，必须存在）→ MacroObservation → MacroDatasetSnapshot → MacroSeries
   → SourceProvider（Source Registry）→ SnapshotArtifact links → RawArtifact；
   链任一断裂 / 快照无 artifact 链接 → EvidenceProvenanceIntegrityError
   （不自动修复）。
2. 纯函数派生（不持有 DB 连接）：
   - provider_key = MacroSeries.provider_key（FK 指向 SourceProvider）；
   - authority_tier_snapshot / critical_claim_eligible_snapshot = 直接复制
     MacroDatasetSnapshot 的获取时快照（**不硬编码 World Bank tier**）；
   - locator_refs = build_macro_observation_locator（deterministic structured
     locator：provider/series/snapshot/observation identity + period）；
   - research_question_sha256 / evidence_fingerprint（macro variant，
     EVIDENCE_SCHEMA_VERSION）。
   - evidence_type 固定 metric；quote 字段固定 NULL；source_published_at /
     reporting_period_end 固定 NULL（macro 无 source record 发布语义）。
3. 短 DB transaction：create_or_get（ON CONFLICT(evidence_fingerprint)，
   无进程锁）→ 首次 created=True → commit；已有 fingerprint → replay 时
   重新加载真实 provenance 并逐项核实（IDs / provider / authority /
   critical / locator / fingerprint），任一损坏 → EvidenceCardIntegrityError，
   **不自动 repair**。修订 = 新 EvidenceCard（statement / extractor version
   / 上游 snapshot 任一变化 → 新 fingerprint → 新行，旧卡保留）。

不读取 Chroma、不重新 Retrieval、不创建 DocumentChunk/Claim/Report/
ReviewIssue。document origin 的卡仍由 EvidenceCardService.create_card 独占。
"""

import uuid
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceOrigin,
    EvidenceType,
    MacroEvidenceDraft,
    build_macro_observation_locator,
    compute_macro_evidence_fingerprint,
    compute_research_question_sha256,
)
from app.evidence.errors import (
    EvidenceCardIntegrityError,
    EvidencePersistenceFailed,
    EvidenceProvenanceIntegrityError,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.macro_series_repository import MacroSeriesRepository
from app.repositories.macro_snapshot_repository import MacroSnapshotRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.evidence_card_service import EvidenceCardResult


@dataclass(frozen=True)
class _MacroProvenance:
    """真实加载的 macro provenance（Company 由调用方上下文 + 显式校验）。"""

    company_id: UUID
    observation: MacroObservationModel
    snapshot: MacroDatasetSnapshotModel
    series: MacroSeriesModel
    provider_key: str


@dataclass(frozen=True)
class _DerivedMacroEvidence:
    """从真实 macro provenance + draft 确定性派生的完整卡字段。"""

    origin_type: str
    company_id: UUID
    macro_observation_id: UUID
    macro_snapshot_id: UUID
    macro_series_id: UUID
    research_question: str
    research_question_sha256: str
    evidence_statement: str
    evidence_type: str
    locator_refs: list[dict]
    provider_key: str
    authority_tier_snapshot: int
    critical_claim_eligible_snapshot: bool
    extractor_name: str
    extractor_version: int
    extractor_model_id: str | None
    extractor_confidence: str
    evidence_schema_version: int
    evidence_fingerprint: str

    def to_model_kwargs(self) -> dict:
        return {
            "origin_type": self.origin_type,
            "company_id": self.company_id,
            "macro_observation_id": self.macro_observation_id,
            "macro_snapshot_id": self.macro_snapshot_id,
            "macro_series_id": self.macro_series_id,
            # document-specific 全部 NULL（macro 不经过 DocumentChunk / quote）。
            "source_id": None,
            "parsed_source_id": None,
            "chunk_set_id": None,
            "chunk_id": None,
            "quote_start": None,
            "quote_end": None,
            "quote_text": None,
            "quote_sha256": None,
            "research_question": self.research_question,
            "research_question_sha256": self.research_question_sha256,
            "evidence_statement": self.evidence_statement,
            "evidence_type": self.evidence_type,
            "locator_refs": self.locator_refs,
            "provider_key": self.provider_key,
            "source_published_at": None,
            "reporting_period_end": None,
            "authority_tier_snapshot": self.authority_tier_snapshot,
            "critical_claim_eligible_snapshot": self.critical_claim_eligible_snapshot,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_model_id": self.extractor_model_id,
            "extractor_confidence": self.extractor_confidence,
            "evidence_schema_version": self.evidence_schema_version,
            "evidence_fingerprint": self.evidence_fingerprint,
        }


class MacroEvidenceService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_macro_card(self, draft: MacroEvidenceDraft) -> EvidenceCardResult:
        # 1. 短 DB session：读真实 macro provenance → 关闭。
        provenance = await self._load_provenance(draft)
        # 2. 纯函数派生（不持有 DB 连接）。
        derived = self._derive(provenance, draft)

        # 3. 短 DB transaction：create_or_get + replay 校验。
        async with self._sessionmaker() as session:
            try:
                repo = EvidenceCardRepository(session)
                existing = await repo.get_by_fingerprint(derived.evidence_fingerprint)
                if existing is not None:
                    self._verify_replay(existing, derived)
                    return self._to_result(existing, replayed=True)
                card = EvidenceCardModel(
                    evidence_card_id=uuid.uuid4(),
                    **derived.to_model_kwargs(),
                )
                card, created = await repo.create_or_get(card)
                if not created:
                    # 并发输家：复用既有卡（replay 校验后返回）。
                    self._verify_replay(card, derived)
                    return self._to_result(card, replayed=True)
                await session.commit()
                return self._to_result(card, replayed=False)
            except SQLAlchemyError as exc:
                await session.rollback()
                raise EvidencePersistenceFailed() from exc

    # ------------------------------------------------------------------ 内部

    async def _load_provenance(self, draft: MacroEvidenceDraft) -> _MacroProvenance:
        """从 draft 真实加载完整 macro provenance；链任一断裂 → IntegrityError。

        Company 必须存在（当前研究公司由调用方上下文提供，Service 显式验证）。
        Snapshot 必须至少有一条 artifact link 且对应 RawArtifact 可加载，
        证明该次获取确有原始响应归档。
        """
        async with self._sessionmaker() as session:
            company_repo = CompanyRepository(session)
            company = await company_repo.get_by_id(draft.company_id)
            if company is None:
                raise EvidenceProvenanceIntegrityError()

            obs_repo = MacroObservationRepository(session)
            observation = await obs_repo.get_by_id(draft.macro_observation_id)
            if observation is None:
                raise EvidenceProvenanceIntegrityError()

            snapshot_repo = MacroSnapshotRepository(session)
            snapshot = await snapshot_repo.get_by_id(observation.snapshot_id)
            if snapshot is None:
                raise EvidenceProvenanceIntegrityError()

            series_repo = MacroSeriesRepository(session)
            series = await series_repo.get_by_id(snapshot.series_id)
            if series is None:
                raise EvidenceProvenanceIntegrityError()

            provider_repo = SourceProviderRepository(session)
            provider = await provider_repo.get_by_key(series.provider_key)
            if provider is None:
                raise EvidenceProvenanceIntegrityError()

            links = await snapshot_repo.list_artifact_links(snapshot.snapshot_id)
            if not links:
                raise EvidenceProvenanceIntegrityError()
            artifact_repo = RawArtifactRepository(session)
            for link in links:
                artifact = await artifact_repo.get_by_id(link.artifact_id)
                if artifact is None:
                    raise EvidenceProvenanceIntegrityError()
        return _MacroProvenance(
            company_id=company.company_id,
            observation=observation,
            snapshot=snapshot,
            series=series,
            provider_key=series.provider_key,
        )

    def _derive(
        self, provenance: _MacroProvenance, draft: MacroEvidenceDraft
    ) -> _DerivedMacroEvidence:
        """deterministic structured locator + sha256 + fingerprint 的确定性派生。

        evidence_type 固定 metric；quote / source_published_at /
        reporting_period_end 固定 NULL；provider / authority tier / critical
        eligibility 一律来自真实 Macro provenance（不硬编码）。
        """
        observation = provenance.observation
        snapshot = provenance.snapshot
        series = provenance.series
        locator_refs = build_macro_observation_locator(
            provider_key=provenance.provider_key,
            series_id=series.series_id,
            snapshot_id=snapshot.snapshot_id,
            observation_id=observation.observation_id,
            source_id=series.source_id,
            external_indicator_id=series.external_indicator_id,
            geography_code=series.geography_code,
            frequency=series.frequency,
            period=observation.period,
            normalized_period_start=observation.normalized_period_start,
        )
        question_sha256 = compute_research_question_sha256(draft.research_question)
        fingerprint = compute_macro_evidence_fingerprint(
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            origin_type=EvidenceOrigin.MACRO_OBSERVATION.value,
            company_id=provenance.company_id,
            research_question=draft.research_question,
            evidence_statement=draft.evidence_statement,
            evidence_type=EvidenceType.METRIC.value,
            macro_observation_id=observation.observation_id,
            macro_snapshot_id=snapshot.snapshot_id,
            macro_series_id=series.series_id,
            period=observation.period,
            normalized_period_start=observation.normalized_period_start,
            value_numeric=observation.value_numeric,
            is_missing=observation.is_missing,
            provider_key=provenance.provider_key,
            authority_tier_snapshot=snapshot.authority_tier_snapshot,
            critical_claim_eligible_snapshot=snapshot.critical_claim_eligible_snapshot,
            locator_refs=locator_refs,
            extractor_name=draft.extractor_name,
            extractor_version=draft.extractor_version,
            extractor_model_id=draft.extractor_model_id,
            extractor_confidence=draft.extractor_confidence.value,
        )
        return _DerivedMacroEvidence(
            origin_type=EvidenceOrigin.MACRO_OBSERVATION.value,
            company_id=provenance.company_id,
            macro_observation_id=observation.observation_id,
            macro_snapshot_id=snapshot.snapshot_id,
            macro_series_id=series.series_id,
            research_question=draft.research_question,
            research_question_sha256=question_sha256,
            evidence_statement=draft.evidence_statement,
            evidence_type=EvidenceType.METRIC.value,
            locator_refs=locator_refs,
            provider_key=provenance.provider_key,
            authority_tier_snapshot=snapshot.authority_tier_snapshot,
            critical_claim_eligible_snapshot=snapshot.critical_claim_eligible_snapshot,
            extractor_name=draft.extractor_name,
            extractor_version=draft.extractor_version,
            extractor_model_id=draft.extractor_model_id,
            extractor_confidence=draft.extractor_confidence.value,
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            evidence_fingerprint=fingerprint,
        )

    @staticmethod
    def _verify_replay(existing: EvidenceCardModel, derived: _DerivedMacroEvidence) -> None:
        """已有 fingerprint 卡的 replay 完整性校验（逐字段比对真实 provenance）。

        任何不一致 → EvidenceCardIntegrityError，**不自动 repair**（修订 =
        新 EvidenceCard）。
        """
        pairs = (
            ("origin_type", existing.origin_type, derived.origin_type),
            ("company_id", existing.company_id, derived.company_id),
            (
                "macro_observation_id",
                existing.macro_observation_id,
                derived.macro_observation_id,
            ),
            ("macro_snapshot_id", existing.macro_snapshot_id, derived.macro_snapshot_id),
            ("macro_series_id", existing.macro_series_id, derived.macro_series_id),
            ("research_question", existing.research_question, derived.research_question),
            (
                "research_question_sha256",
                existing.research_question_sha256,
                derived.research_question_sha256,
            ),
            ("evidence_statement", existing.evidence_statement, derived.evidence_statement),
            ("evidence_type", existing.evidence_type, derived.evidence_type),
            ("locator_refs", existing.locator_refs, derived.locator_refs),
            ("provider_key", existing.provider_key, derived.provider_key),
            (
                "authority_tier_snapshot",
                existing.authority_tier_snapshot,
                derived.authority_tier_snapshot,
            ),
            (
                "critical_claim_eligible_snapshot",
                existing.critical_claim_eligible_snapshot,
                derived.critical_claim_eligible_snapshot,
            ),
            ("extractor_name", existing.extractor_name, derived.extractor_name),
            ("extractor_version", existing.extractor_version, derived.extractor_version),
            ("extractor_model_id", existing.extractor_model_id, derived.extractor_model_id),
            ("extractor_confidence", existing.extractor_confidence, derived.extractor_confidence),
            (
                "evidence_schema_version",
                existing.evidence_schema_version,
                derived.evidence_schema_version,
            ),
            ("evidence_fingerprint", existing.evidence_fingerprint, derived.evidence_fingerprint),
        )
        for name, stored, expected in pairs:
            if stored != expected:
                raise EvidenceCardIntegrityError(
                    f"macro evidence card replay integrity check failed on {name}"
                )

    @staticmethod
    def _to_result(card: EvidenceCardModel, *, replayed: bool) -> EvidenceCardResult:
        return EvidenceCardResult(
            evidence_card_id=card.evidence_card_id,
            chunk_id=None,
            evidence_fingerprint=card.evidence_fingerprint,
            replayed=replayed,
        )
