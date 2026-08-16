"""Financial extraction evidence card service (F1).

把通过 numeric provenance 校验的自动提取观测登记为 FINANCIAL_EXTRACTION
EvidenceCard：quote = ParsedSourceBlock 文本的逐字切片（含精确数字 token），
source = **原始报告 SourceRecord**（authority tier / critical_claim_eligible
快照继承报告来源，不硬编码、不伪装可信级别）；extractor 身份固定为
financial_extraction v1 / low（确定性提取，非 LLM）。

幂等：同一 fingerprint → replay 同一卡并逐项校验（tamper → IntegrityError）。
"""

import uuid
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.parsed_source_block import ParsedSourceBlockModel
from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import (
    EVIDENCE_SCHEMA_VERSION,
    EvidenceOrigin,
    FinancialExtractionEvidenceDraft,
    build_financial_extraction_locator,
    compute_financial_extraction_evidence_fingerprint,
    compute_quote_sha256,
    compute_research_question_sha256,
)
from app.evidence.errors import (
    EvidenceCardIntegrityError,
    EvidencePersistenceFailed,
)
from app.repositories.evidence_card_repository import EvidenceCardRepository

FINANCIAL_EXTRACTION_EXTRACTOR_NAME = "financial_extraction"
# v2：locator 从 block-only 升级为 block + page_number + line_index（P8
# Evidence Locator）——fingerprint 覆盖 locator 差异，旧卡不复用。
FINANCIAL_EXTRACTION_EXTRACTOR_VERSION = 2
FINANCIAL_EXTRACTION_EXTRACTOR_CONFIDENCE = "low"


@dataclass(frozen=True)
class FinancialExtractionEvidenceResult:
    """一次 create_card 的结果摘要（不含正文 / locator 细节）。"""

    evidence_card_id: UUID
    evidence_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class _DerivedEvidence:
    """从真实 SourceRecord provenance + draft 确定性派生的完整卡字段。"""

    origin_type: str
    company_id: UUID
    source_id: UUID
    parsed_source_id: UUID
    research_question: str
    research_question_sha256: str
    evidence_statement: str
    evidence_type: str
    quote_text: str
    quote_sha256: str
    quote_start: int
    quote_end: int
    locator_refs: list[dict]
    provider_key: str
    source_published_at: date | None
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
            "research_question": self.research_question,
            "research_question_sha256": self.research_question_sha256,
            "evidence_statement": self.evidence_statement,
            "evidence_type": self.evidence_type,
            "quote_text": self.quote_text,
            "quote_sha256": self.quote_sha256,
            "quote_start": self.quote_start,
            "quote_end": self.quote_end,
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


class FinancialExtractionEvidenceService:
    """自动财务提取证据卡登记（幂等、可追溯、tier 继承报告来源）。"""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def create_card(
        self, draft: FinancialExtractionEvidenceDraft
    ) -> FinancialExtractionEvidenceResult:
        async with self._sessionmaker() as session:
            try:
                record = await self._load_validate_source(session, draft)
                # P8 Evidence Locator：从 ParsedSourceBlock 的真实 locator
                # （pdf_page 解析产生 page_number / line_index）填充——不是
                # LLM 猜测，是解析链的真实位置。
                block = await session.get(ParsedSourceBlockModel, draft.quote_block_id)
                page_number, line_index = self._block_page_line(block)
                derived = self._derive(
                    record,
                    draft,
                    page_number=page_number,
                    line_index=line_index,
                )
                repo = EvidenceCardRepository(session)
                existing = await repo.get_by_fingerprint(derived.evidence_fingerprint)
                if existing is not None:
                    self._verify_replay(existing, derived)
                    return FinancialExtractionEvidenceResult(
                        evidence_card_id=existing.evidence_card_id,
                        evidence_fingerprint=derived.evidence_fingerprint,
                        replayed=True,
                    )
                card = EvidenceCardModel(
                    evidence_card_id=uuid.uuid4(),
                    **derived.to_model_kwargs(),
                )
                card, created = await repo.create_or_get(card)
                if not created:
                    self._verify_replay(card, derived)
                    return FinancialExtractionEvidenceResult(
                        evidence_card_id=card.evidence_card_id,
                        evidence_fingerprint=derived.evidence_fingerprint,
                        replayed=True,
                    )
                await session.commit()
                return FinancialExtractionEvidenceResult(
                    evidence_card_id=card.evidence_card_id,
                    evidence_fingerprint=derived.evidence_fingerprint,
                    replayed=False,
                )
            except SQLAlchemyError as exc:
                await session.rollback()
                raise EvidencePersistenceFailed() from exc

    # ------------------------------------------------------------ internal

    @staticmethod
    async def _load_validate_source(
        session: AsyncSession, draft: FinancialExtractionEvidenceDraft
    ) -> SourceRecordModel:
        """加载原始报告 SourceRecord（缺失 / 跨公司 → IntegrityError）。"""
        record = await session.get(SourceRecordModel, draft.source_id)
        if record is None:
            raise EvidenceCardIntegrityError("financial extraction source record missing")
        if record.company_id != draft.company_id:
            raise EvidenceCardIntegrityError("financial extraction source company mismatch")
        return record

    @staticmethod
    def _block_page_line(block) -> tuple[int | None, int | None]:
        """block.locator（真实解析链）→ page_number / line_index（缺省 None）。"""
        if block is None:
            return None, None
        locator = block.locator or {}
        if not isinstance(locator, dict):
            return None, None
        page = locator.get("page_number")
        line = locator.get("line_index")
        return (
            page if isinstance(page, int) and page > 0 else None,
            line if isinstance(line, int) and line >= 0 else None,
        )

    @staticmethod
    def _derive(
        record: SourceRecordModel,
        draft: FinancialExtractionEvidenceDraft,
        *,
        page_number: int | None = None,
        line_index: int | None = None,
    ):
        locator = build_financial_extraction_locator(
            source_id=draft.source_id,
            parsed_source_id=draft.parsed_source_id,
            block_id=draft.quote_block_id,
            page_number=page_number,
            line_index=line_index,
        )
        fingerprint = compute_financial_extraction_evidence_fingerprint(
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            origin_type=EvidenceOrigin.FINANCIAL_EXTRACTION.value,
            company_id=draft.company_id,
            source_id=draft.source_id,
            parsed_source_id=draft.parsed_source_id,
            quote_block_id=draft.quote_block_id,
            research_question=draft.research_question,
            evidence_statement=draft.evidence_statement,
            evidence_type=draft.evidence_type.value,
            quote_text=draft.quote_text,
            quote_sha256=compute_quote_sha256(draft.quote_text),
            quote_start=draft.quote_start,
            quote_end=draft.quote_end,
            locator_refs=locator,
            provider_key=record.provider_key,
            authority_tier_snapshot=int(record.authority_tier_snapshot or 0),
            critical_claim_eligible_snapshot=bool(record.critical_claim_eligible_snapshot),
            reporting_period_end=record.reporting_period_end,
            extractor_name=FINANCIAL_EXTRACTION_EXTRACTOR_NAME,
            extractor_version=FINANCIAL_EXTRACTION_EXTRACTOR_VERSION,
            extractor_model_id=None,
            extractor_confidence=FINANCIAL_EXTRACTION_EXTRACTOR_CONFIDENCE,
        )
        return _DerivedEvidence(
            origin_type=EvidenceOrigin.FINANCIAL_EXTRACTION.value,
            company_id=draft.company_id,
            source_id=draft.source_id,
            parsed_source_id=draft.parsed_source_id,
            research_question=draft.research_question,
            research_question_sha256=compute_research_question_sha256(draft.research_question),
            evidence_statement=draft.evidence_statement,
            evidence_type=draft.evidence_type.value,
            quote_text=draft.quote_text,
            quote_sha256=compute_quote_sha256(draft.quote_text),
            quote_start=draft.quote_start,
            quote_end=draft.quote_end,
            locator_refs=locator,
            provider_key=record.provider_key,
            source_published_at=record.published_at,
            reporting_period_end=record.reporting_period_end,
            authority_tier_snapshot=int(record.authority_tier_snapshot or 0),
            critical_claim_eligible_snapshot=bool(record.critical_claim_eligible_snapshot),
            extractor_name=FINANCIAL_EXTRACTION_EXTRACTOR_NAME,
            extractor_version=FINANCIAL_EXTRACTION_EXTRACTOR_VERSION,
            extractor_model_id=None,
            extractor_confidence=FINANCIAL_EXTRACTION_EXTRACTOR_CONFIDENCE,
            evidence_schema_version=EVIDENCE_SCHEMA_VERSION,
            evidence_fingerprint=fingerprint,
        )

    @staticmethod
    def _verify_replay(card: EvidenceCardModel, derived: _DerivedEvidence) -> None:
        """replay 完整性校验：指纹 + 关键字段一致（tamper → IntegrityError）。"""
        if card.evidence_fingerprint != derived.evidence_fingerprint:
            raise EvidenceCardIntegrityError("financial extraction evidence fingerprint mismatch")
        if card.quote_text != derived.quote_text or card.quote_sha256 != derived.quote_sha256:
            raise EvidenceCardIntegrityError("financial extraction evidence quote tampered")
        if card.origin_type != derived.origin_type:
            raise EvidenceCardIntegrityError("financial extraction evidence origin tampered")
        if card.source_id != derived.source_id:
            raise EvidenceCardIntegrityError("financial extraction evidence source tampered")
