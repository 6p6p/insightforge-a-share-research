"""Evidence card service (stage 3C.1): deterministic provenance + quote + persistence.

create_card(draft) 把"已确认与研究问题相关的语义输入"确定性登记为一张
EvidenceCard。**无 LLM、无 Evidence Extractor Agent、无 Claim 创建**：
调用方显式提交 EvidenceCardDraft，Service 只做确定性派生与幂等落库。

流程：
1. 短 DB session 读真实 provenance：DocumentChunk → ChunkSet → ParsedSource
   → SourceRecord（Company 由 SourceRecord.company_id FK 保证存在）→ 关闭；
   链任一断裂 → EvidenceProvenanceIntegrityError（不自动修复）。
2. 纯函数派生（不持有 DB 连接）：
   - quote_text = chunk.text[quote_start:quote_end]（程序切片，越界/空白 →
     EvidenceQuoteRangeError；绝不 normalize/改写/摘要）；
   - locator_refs = project_evidence_locator_refs（quote 级投影）；
   - research_question_sha256 / quote_sha256；
   - evidence_fingerprint = canonical JSON + SHA-256。
3. 短 DB transaction：create_or_get（ON CONFLICT(evidence_fingerprint)，
   无进程锁）→ 首次 created=True → commit；已有 fingerprint → replay 时
   **重新加载真实 provenance 并逐项核实**（quote slice / sha256 / locator /
   IDs / provider / authority tier / critical eligibility / published /
   reporting period / fingerprint），任一损坏 → EvidenceCardIntegrityError，
   **不自动 repair**。修订 = 新 EvidenceCard（语义/quote/extractor version
   任一变化 → 新 fingerprint → 新行，旧卡保留）。

不读取 Chroma、不重新 Retrieval、不创建 Claim/Report/ReviewIssue。
"""

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceCardDraft,
    EvidenceOrigin,
    compute_evidence_fingerprint,
    compute_quote_sha256,
    compute_research_question_sha256,
    derive_quote_text,
    project_evidence_locator_refs,
)
from app.evidence.errors import (
    EvidenceCardIntegrityError,
    EvidencePersistenceFailed,
    EvidenceProvenanceIntegrityError,
)
from app.repositories.chunk_set_repository import ChunkSetRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.repositories.source_record_repository import SourceRecordRepository


@dataclass(frozen=True)
class EvidenceCardResult:
    """一次 create_card 的结果摘要（不含任何正文文本 / locator）。

    chunk_id：document_chunk origin 时为实际 chunk_id；macro_observation
    origin 时为 None（macro Evidence 不经过 DocumentChunk）。
    """

    evidence_card_id: UUID
    chunk_id: UUID | None
    evidence_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _Provenance:
    """真实加载的证据链 provenance（Company 由 SourceRecord FK 保证存在）。"""

    chunk: DocumentChunkModel
    chunk_set_id: UUID
    parsed_source_id: UUID
    source_id: UUID
    company_id: UUID
    provider_key: str
    source_published_at: datetime | None
    reporting_period_end: date | None
    authority_tier_snapshot: int
    critical_claim_eligible_snapshot: bool


@dataclass(frozen=True)
class _DerivedEvidence:
    """从真实 provenance + draft 确定性派生的完整卡字段（document origin）。"""

    origin_type: str
    company_id: UUID
    source_id: UUID
    parsed_source_id: UUID
    chunk_set_id: UUID
    chunk_id: UUID
    research_question: str
    research_question_sha256: str
    evidence_statement: str
    evidence_type: str
    quote_start: int
    quote_end: int
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
            "parsed_source_id": self.parsed_source_id,
            "chunk_set_id": self.chunk_set_id,
            "chunk_id": self.chunk_id,
            "research_question": self.research_question,
            "research_question_sha256": self.research_question_sha256,
            "evidence_statement": self.evidence_statement,
            "evidence_type": self.evidence_type,
            "quote_start": self.quote_start,
            "quote_end": self.quote_end,
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


class EvidenceCardService:
    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_card(self, draft: EvidenceCardDraft) -> EvidenceCardResult:
        # 1. 短 DB session：读真实 provenance → 关闭。
        provenance = await self._load_provenance(draft.chunk_id)
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

    async def _load_provenance(self, chunk_id: UUID) -> _Provenance:
        """从 chunk_id 真实加载完整 provenance；链任一断裂 → IntegrityError。"""
        async with self._sessionmaker() as session:
            chunk_repo = DocumentChunkRepository(session)
            chunk = await chunk_repo.get_by_id(chunk_id)
            if chunk is None:
                raise EvidenceProvenanceIntegrityError()
            set_repo = ChunkSetRepository(session)
            chunk_set = await set_repo.get_by_id(chunk.chunk_set_id)
            if chunk_set is None:
                raise EvidenceProvenanceIntegrityError()
            parsed_repo = ParsedSourceRepository(session)
            parsed = await parsed_repo.get_by_id(chunk_set.parsed_source_id)
            if parsed is None:
                raise EvidenceProvenanceIntegrityError()
            record_repo = SourceRecordRepository(session)
            record = await record_repo.get_by_id(parsed.source_id)
            if record is None:
                raise EvidenceProvenanceIntegrityError()
        return _Provenance(
            chunk=chunk,
            chunk_set_id=chunk_set.chunk_set_id,
            parsed_source_id=parsed.parsed_source_id,
            source_id=record.source_id,
            company_id=record.company_id,
            provider_key=record.provider_key,
            source_published_at=record.published_at,
            reporting_period_end=record.reporting_period_end,
            authority_tier_snapshot=record.authority_tier_snapshot,
            critical_claim_eligible_snapshot=record.critical_claim_eligible_snapshot,
        )

    def _derive(self, provenance: _Provenance, draft: EvidenceCardDraft) -> _DerivedEvidence:
        """quote 切片 / locator 投影 / sha256 / fingerprint 的确定性派生。"""
        quote_text = derive_quote_text(
            chunk_text=provenance.chunk.text,
            quote_start=draft.quote_start,
            quote_end=draft.quote_end,
        )
        quote_sha256 = compute_quote_sha256(quote_text)
        locator_refs = project_evidence_locator_refs(
            provenance.chunk.text,
            provenance.chunk.locator_refs,
            draft.quote_start,
            draft.quote_end,
        )
        question_sha256 = compute_research_question_sha256(draft.research_question)
        fingerprint = compute_evidence_fingerprint(
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
            company_id=provenance.company_id,
            source_id=provenance.source_id,
            parsed_source_id=provenance.parsed_source_id,
            chunk_set_id=provenance.chunk_set_id,
            chunk_id=provenance.chunk.chunk_id,
            research_question=draft.research_question,
            evidence_statement=draft.evidence_statement,
            evidence_type=draft.evidence_type.value,
            quote_start=draft.quote_start,
            quote_end=draft.quote_end,
            quote_sha256=quote_sha256,
            locator_refs=locator_refs,
            provider_key=provenance.provider_key,
            source_published_at=provenance.source_published_at,
            reporting_period_end=provenance.reporting_period_end,
            authority_tier_snapshot=provenance.authority_tier_snapshot,
            critical_claim_eligible_snapshot=provenance.critical_claim_eligible_snapshot,
            extractor_name=draft.extractor_name,
            extractor_version=draft.extractor_version,
            extractor_model_id=draft.extractor_model_id,
            extractor_confidence=draft.extractor_confidence.value,
        )
        return _DerivedEvidence(
            origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
            company_id=provenance.company_id,
            source_id=provenance.source_id,
            parsed_source_id=provenance.parsed_source_id,
            chunk_set_id=provenance.chunk_set_id,
            chunk_id=provenance.chunk.chunk_id,
            research_question=draft.research_question,
            research_question_sha256=question_sha256,
            evidence_statement=draft.evidence_statement,
            evidence_type=draft.evidence_type.value,
            quote_start=draft.quote_start,
            quote_end=draft.quote_end,
            quote_text=quote_text,
            quote_sha256=quote_sha256,
            locator_refs=locator_refs,
            provider_key=provenance.provider_key,
            source_published_at=provenance.source_published_at,
            reporting_period_end=provenance.reporting_period_end,
            authority_tier_snapshot=provenance.authority_tier_snapshot,
            critical_claim_eligible_snapshot=provenance.critical_claim_eligible_snapshot,
            extractor_name=draft.extractor_name,
            extractor_version=draft.extractor_version,
            extractor_model_id=draft.extractor_model_id,
            extractor_confidence=draft.extractor_confidence.value,
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            evidence_fingerprint=fingerprint,
        )

    @staticmethod
    def _verify_replay(existing: EvidenceCardModel, derived: _DerivedEvidence) -> None:
        """已有 fingerprint 卡的 replay 完整性校验（逐字段比对真实 provenance）。

        任何不一致 → EvidenceCardIntegrityError，**不自动 repair**（修订 =
        新 EvidenceCard）。
        """
        pairs = (
            ("origin_type", existing.origin_type, derived.origin_type),
            ("company_id", existing.company_id, derived.company_id),
            ("source_id", existing.source_id, derived.source_id),
            ("parsed_source_id", existing.parsed_source_id, derived.parsed_source_id),
            ("chunk_set_id", existing.chunk_set_id, derived.chunk_set_id),
            ("chunk_id", existing.chunk_id, derived.chunk_id),
            ("research_question", existing.research_question, derived.research_question),
            (
                "research_question_sha256",
                existing.research_question_sha256,
                derived.research_question_sha256,
            ),
            ("evidence_statement", existing.evidence_statement, derived.evidence_statement),
            ("evidence_type", existing.evidence_type, derived.evidence_type),
            ("quote_start", existing.quote_start, derived.quote_start),
            ("quote_end", existing.quote_end, derived.quote_end),
            ("quote_text", existing.quote_text, derived.quote_text),
            ("quote_sha256", existing.quote_sha256, derived.quote_sha256),
            ("locator_refs", existing.locator_refs, derived.locator_refs),
            ("provider_key", existing.provider_key, derived.provider_key),
            (
                "source_published_at",
                existing.source_published_at,
                derived.source_published_at,
            ),
            ("reporting_period_end", existing.reporting_period_end, derived.reporting_period_end),
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
                    f"evidence card replay integrity check failed on {name}"
                )

    @staticmethod
    def _to_result(card: EvidenceCardModel, *, replayed: bool) -> EvidenceCardResult:
        return EvidenceCardResult(
            evidence_card_id=card.evidence_card_id,
            chunk_id=card.chunk_id,
            evidence_fingerprint=card.evidence_fingerprint,
            replayed=replayed,
        )
