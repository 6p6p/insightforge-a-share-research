"""User-supplied evidence service (V1.1 final closure): 用户转录 → EvidenceCard.

用户从官方报告 / 官网公告**人工转录**的 Evidence 的确定性登记（stage
3C.1 的 user_supplied origin 路径）：

1. user_supplied RawArtifact：内容是用户提交的**确定性 JSON 收据**
   （kind / source_title / source_url / document_type / quote_text /
   evidence_statement / evidence_type / company_id），**不是伪造的文件
   内容**；content_sha256 只由确定性字段派生 → 相同提交 → 相同 sha256
   → 同一 artifact。
2. user_supplied SourceRecord（provider_key='user_supplied'，Tier-4、
   critical_claim_eligible=False，acquisition_method=user_supplied，
   source_url 可为 None——用户可能只有 PDF / 口头来源）。
3. user_supplied EvidenceCard（origin_type='user_supplied'，quote = 用户
   粘贴的原文引文，locator = structured user_supplied locator，replay
   幂等：同一 fingerprint → 复用既有卡并逐项校验）。

**绝不伪装成官方自动提取**：authority_tier_snapshot / critical_claim_
eligible_snapshot 复制自真实 user_supplied provider 行（缺失 → 拒绝登记，
不硬编码可信级别）；extractor 身份固定为 user_transcription v1 /
confidence=low。
"""

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.domain.source_records import RawArtifactMediaType, SourceRecordStatus
from app.domain.sources import AcquisitionMethod
from app.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceOrigin,
    UserSuppliedEvidenceDraft,
    build_user_supplied_locator,
    compute_quote_sha256,
    compute_research_question_sha256,
    compute_user_supplied_evidence_fingerprint,
)
from app.evidence.errors import (
    EvidenceCardIntegrityError,
    EvidencePersistenceFailed,
    EvidenceProviderNotRegisteredError,
)
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository

# user_supplied provider key（seed_defaults 中登记；缺失时服务拒绝登记）。
USER_SUPPLIED_PROVIDER_KEY = "user_supplied"
# extractor 身份：用户人工转录（无自动提取），固定 v1 / low。
USER_SUPPLIED_EXTRACTOR_NAME = "user_transcription"
USER_SUPPLIED_EXTRACTOR_VERSION = 1
USER_SUPPLIED_EXTRACTOR_CONFIDENCE = "low"

_ARTIFACT_KIND = "user_supplied_transcription"
_ARTIFACT_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class UserSuppliedEvidenceResult:
    """一次 create_card 的结果摘要（不含任何正文文本 / locator）。"""

    evidence_card_id: UUID
    source_id: UUID
    artifact_id: UUID
    evidence_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _DerivedEvidence:
    """从真实 provider/source provenance + draft 确定性派生的完整卡字段。"""

    origin_type: str
    company_id: UUID
    source_id: UUID
    research_question: str
    research_question_sha256: str
    evidence_statement: str
    evidence_type: str
    quote_text: str
    quote_sha256: str
    locator_refs: list[dict]
    provider_key: str
    source_published_at: datetime | None
    reporting_period_end: date | None
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
            "source_id": self.source_id,
            "research_question": self.research_question,
            "research_question_sha256": self.research_question_sha256,
            "evidence_statement": self.evidence_statement,
            "evidence_type": self.evidence_type,
            "quote_text": self.quote_text,
            "quote_sha256": self.quote_sha256,
            "locator_refs": self.locator_refs,
            "provider_key": self.provider_key,
            "source_published_at": self.source_published_at,
            "reporting_period_end": self.reporting_period_end,
            "authority_tier_snapshot": self.authority_tier_snapshot,
            "critical_claim_eligible_snapshot": self.critical_claim_eligible_snapshot,
            "extractor_name": self.extractor_name,
            "extractor_version": self.extractor_version,
            "extractor_model_id": self.extractor_model_id,
            "extractor_confidence": self.extractor_confidence,
            "evidence_schema_version": self.evidence_schema_version,
            "evidence_fingerprint": self.evidence_fingerprint,
        }


def _build_artifact_payload(draft: UserSuppliedEvidenceDraft) -> dict:
    """确定性 JSON 收据（只含确定性字段，不含时间戳 → 幂等 sha256）。"""
    return {
        "kind": _ARTIFACT_KIND,
        "schema_version": _ARTIFACT_SCHEMA_VERSION,
        "company_id": str(draft.company_id),
        "source_title": draft.source_title,
        "source_url": draft.source_url,
        "document_type": draft.document_type.value,
        "quote_text": draft.quote_text,
        "evidence_statement": draft.evidence_statement,
        "evidence_type": draft.evidence_type.value,
    }


class UserSuppliedEvidenceService:
    """用户转录 Evidence 的确定性登记（幂等、可追溯、不伪装官方）。"""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_card(self, draft: UserSuppliedEvidenceDraft) -> UserSuppliedEvidenceResult:
        async with self._sessionmaker() as session:
            try:
                provider = await self._load_provider(session)
                artifact = await self._ensure_artifact(session, draft)
                record = await self._ensure_source_record(session, provider, artifact, draft)
                derived = self._derive(provider, record, draft)
                repo = EvidenceCardRepository(session)
                existing = await repo.get_by_fingerprint(derived.evidence_fingerprint)
                if existing is not None:
                    self._verify_replay(existing, derived)
                    return self._to_result(existing, record, artifact, replayed=True)
                card = EvidenceCardModel(
                    evidence_card_id=uuid.uuid4(),
                    **derived.to_model_kwargs(),
                )
                card, created = await repo.create_or_get(card)
                if not created:
                    # 并发输家：复用既有卡（replay 校验后返回）。
                    self._verify_replay(card, derived)
                    return self._to_result(card, record, artifact, replayed=True)
                await session.commit()
                return self._to_result(card, record, artifact, replayed=False)
            except SQLAlchemyError as exc:
                await session.rollback()
                raise EvidencePersistenceFailed() from exc

    # ------------------------------------------------------------------ 内部

    @staticmethod
    async def _load_provider(session) -> SourceProviderModel:
        repo = SourceProviderRepository(session)
        provider = await repo.get_by_key(USER_SUPPLIED_PROVIDER_KEY)
        if provider is None:
            raise EvidenceProviderNotRegisteredError()
        return provider

    async def _ensure_artifact(
        self, session, draft: UserSuppliedEvidenceDraft
    ) -> RawArtifactModel:
        payload = _build_artifact_payload(draft)
        canonical = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        content_sha256 = hashlib.sha256(canonical).hexdigest()
        artifact = RawArtifactModel(
            artifact_id=uuid.uuid4(),
            content_sha256=content_sha256,
            storage_key=f"user_supplied/{draft.company_id}/{content_sha256[:32]}.json",
            byte_size=len(canonical),
            media_type=RawArtifactMediaType.JSON.value,
        )
        existing, _ = await RawArtifactRepository(session).get_or_create(artifact)
        return existing

    async def _ensure_source_record(
        self,
        session,
        provider: SourceProviderModel,
        artifact: RawArtifactModel,
        draft: UserSuppliedEvidenceDraft,
    ) -> SourceRecordModel:
        """按 (provider_key, artifact_id) 幂等取/建 user_supplied SourceRecord。

        `source_url` 可为 NULL（用户可能只有 PDF），所以不复用
        (provider_key, source_url, artifact_id) 冲突索引——NULL 在 PG 唯一
        索引中互不冲突；显式按 (provider_key, artifact_id) 查重。
        """
        existing = (
            await session.execute(
                select(SourceRecordModel).where(
                    SourceRecordModel.provider_key == USER_SUPPLIED_PROVIDER_KEY,
                    SourceRecordModel.artifact_id == artifact.artifact_id,
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        record = SourceRecordModel(
            source_id=uuid.uuid4(),
            company_id=draft.company_id,
            provider_key=USER_SUPPLIED_PROVIDER_KEY,
            artifact_id=artifact.artifact_id,
            document_type=draft.document_type.value,
            title=draft.source_title,
            published_at=draft.source_published_at,
            reporting_period_end=draft.reporting_period_end,
            source_url=draft.source_url,
            acquisition_method=AcquisitionMethod.USER_SUPPLIED.value,
            external_document_id=None,
            authority_tier_snapshot=provider.authority_tier,
            critical_claim_eligible_snapshot=provider.critical_claim_eligible,
            provider_capabilities_snapshot=provider.capabilities,
            status=SourceRecordStatus.AVAILABLE.value,
            acquired_at=datetime.now(UTC),
        )
        await SourceRecordRepository(session).create(record)
        return record

    def _derive(
        self,
        provider: SourceProviderModel,
        record: SourceRecordModel,
        draft: UserSuppliedEvidenceDraft,
    ) -> _DerivedEvidence:
        quote_sha256 = compute_quote_sha256(draft.quote_text)
        question_sha256 = compute_research_question_sha256(draft.research_question)
        locator_refs = build_user_supplied_locator(
            source_id=record.source_id, source_url=record.source_url
        )
        fingerprint = compute_user_supplied_evidence_fingerprint(
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            origin_type=EvidenceOrigin.USER_SUPPLIED.value,
            company_id=draft.company_id,
            source_id=record.source_id,
            research_question=draft.research_question,
            evidence_statement=draft.evidence_statement,
            evidence_type=draft.evidence_type.value,
            quote_text=draft.quote_text,
            quote_sha256=quote_sha256,
            locator_refs=locator_refs,
            provider_key=record.provider_key,
            authority_tier_snapshot=provider.authority_tier,
            critical_claim_eligible_snapshot=provider.critical_claim_eligible,
            source_url=record.source_url,
            source_published_at=record.published_at,
            reporting_period_end=record.reporting_period_end,
            extractor_name=USER_SUPPLIED_EXTRACTOR_NAME,
            extractor_version=USER_SUPPLIED_EXTRACTOR_VERSION,
            extractor_model_id=None,
            extractor_confidence=USER_SUPPLIED_EXTRACTOR_CONFIDENCE,
        )
        return _DerivedEvidence(
            origin_type=EvidenceOrigin.USER_SUPPLIED.value,
            company_id=draft.company_id,
            source_id=record.source_id,
            research_question=draft.research_question,
            research_question_sha256=question_sha256,
            evidence_statement=draft.evidence_statement,
            evidence_type=draft.evidence_type.value,
            quote_text=draft.quote_text,
            quote_sha256=quote_sha256,
            locator_refs=locator_refs,
            provider_key=record.provider_key,
            source_published_at=record.published_at,
            reporting_period_end=record.reporting_period_end,
            authority_tier_snapshot=provider.authority_tier,
            critical_claim_eligible_snapshot=provider.critical_claim_eligible,
            extractor_name=USER_SUPPLIED_EXTRACTOR_NAME,
            extractor_version=USER_SUPPLIED_EXTRACTOR_VERSION,
            extractor_model_id=None,
            extractor_confidence=USER_SUPPLIED_EXTRACTOR_CONFIDENCE,
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            evidence_fingerprint=fingerprint,
        )

    @staticmethod
    def _verify_replay(
        existing: EvidenceCardModel, derived: _DerivedEvidence
    ) -> None:
        """已有 fingerprint 卡的 replay 完整性校验（逐字段比对真实 provenance）。"""
        pairs = (
            ("origin_type", existing.origin_type, derived.origin_type),
            ("company_id", existing.company_id, derived.company_id),
            ("source_id", existing.source_id, derived.source_id),
            ("research_question", existing.research_question, derived.research_question),
            (
                "research_question_sha256",
                existing.research_question_sha256,
                derived.research_question_sha256,
            ),
            ("evidence_statement", existing.evidence_statement, derived.evidence_statement),
            ("evidence_type", existing.evidence_type, derived.evidence_type),
            ("quote_text", existing.quote_text, derived.quote_text),
            ("quote_sha256", existing.quote_sha256, derived.quote_sha256),
            ("locator_refs", existing.locator_refs, derived.locator_refs),
            ("provider_key", existing.provider_key, derived.provider_key),
            (
                "source_published_at",
                existing.source_published_at,
                derived.source_published_at,
            ),
            (
                "reporting_period_end",
                existing.reporting_period_end,
                derived.reporting_period_end,
            ),
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
                    f"user supplied evidence card replay integrity check failed on {name}"
                )

    @staticmethod
    def _to_result(
        card: EvidenceCardModel,
        record: SourceRecordModel,
        artifact: RawArtifactModel,
        *,
        replayed: bool,
    ) -> UserSuppliedEvidenceResult:
        return UserSuppliedEvidenceResult(
            evidence_card_id=card.evidence_card_id,
            source_id=record.source_id,
            artifact_id=artifact.artifact_id,
            evidence_fingerprint=card.evidence_fingerprint,
            replayed=replayed,
        )
