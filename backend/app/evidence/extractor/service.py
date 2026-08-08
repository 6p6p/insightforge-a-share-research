"""Evidence extraction service (stage 3C.2): RetrievalHit → LLM → EvidenceCard.

extract_from_hit(research_question, hit) 流程：
1. 校验 research_question（trim 后非空，空 → EvidenceExtractionInputError）。
2. 短 DB read：按 hit.chunk_id 加载当前 DocumentChunk + provenance 并做
   stale 校验（sha256(hit.text) == chunk.text_sha256 且 5 个 ids 与当前
   provenance 一致）；不一致 → EvidenceExtractionInputStale，**不基于 stale
   hit 创建 Evidence**。
3. 调 EvidenceExtractionModel.extract(research_question, hit)（provider 失败
   由 adapter 翻译为 EvidenceExtractorUnavailable）。
4. strict schema validation（Pydantic model_validate）。
5. relevant=false → no_evidence（DB 0 写）。
6. 全部 items 先完成 quote 解析与 EvidenceCardDraft 构造（quote resolver +
   draft 校验）；**validation 完成前不得创建 EvidenceCard**。
7. 每个 draft 调 EvidenceCardService.create_card（fingerprint / replay / 并发
   由 3C.1 保证）；单 Hit 最多 3 卡。

Extractor 不调用 RetrievalService、不读 Chroma、不重新 Retrieval、不创建
Claim / Report / ReviewIssue、无 LangGraph / CrewAI / reranker / 第二个 judge。
"""

import hashlib
import time
from dataclasses import dataclass
from uuid import UUID

import structlog
from pydantic import ValidationError

from app.evidence.contracts import EvidenceCardDraft
from app.evidence.errors import EvidenceError
from app.evidence.extractor.contracts import (
    EVIDENCE_EXTRACTOR_NAME,
    EVIDENCE_EXTRACTOR_VERSION,
    EvidenceExtractionDecision,
    EvidenceExtractionModel,
    EvidenceExtractionReason,
)
from app.evidence.extractor.errors import (
    EvidenceExtractionInputError,
    EvidenceExtractionInputStale,
    EvidenceExtractionMalformedOutput,
    EvidenceExtractorUnavailable,
)
from app.evidence.extractor.quote import resolve_exact_quote
from app.rag.retrieval.contracts import RetrievalHit
from app.repositories.chunk_set_repository import ChunkSetRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.evidence_card_service import EvidenceCardResult, EvidenceCardService

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class EvidenceExtractionResult:
    """一次 extract_from_hit 的结果摘要（不含正文 / quote / locator 文本）。"""

    relevant: bool
    evidence_card_ids: list[UUID]
    created_count: int
    replayed_count: int
    reason_code: EvidenceExtractionReason | None


@dataclass(frozen=True)
class _FreshChunk:
    """stale 校验后从 PG 加载的当前 chunk + provenance（quote 以它为准）。"""

    chunk_id: UUID
    chunk_set_id: UUID
    parsed_source_id: UUID
    source_id: UUID
    company_id: UUID
    text: str
    text_sha256: str


class EvidenceExtractionService:
    def __init__(
        self,
        sessionmaker,
        model: EvidenceExtractionModel,
        card_service: EvidenceCardService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._model = model
        model_id = getattr(model, "model_id", None)
        if not isinstance(model_id, str) or not model_id:
            raise EvidenceExtractorUnavailable("extractor model 未提供稳定 model_id")
        if not callable(getattr(model, "extract", None)):
            raise EvidenceExtractorUnavailable("extractor model 未实现 extract()")
        self._model_id = model_id
        self._card_service = (
            card_service if card_service is not None else EvidenceCardService(sessionmaker)
        )

    async def extract_from_hit(
        self, research_question: str, hit: RetrievalHit
    ) -> EvidenceExtractionResult:
        started = time.monotonic()
        question = self._validate_research_question(research_question)
        try:
            # 2. 短 DB read + stale 校验（在调用 LLM 前完成）。
            fresh = await self._load_fresh_chunk(hit)
            # 3. LLM 结构化抽取。
            decision = await self._model.extract(question, hit)
            # 4. strict schema validation。
            validated = self._validate_decision(decision)
            if not validated.relevant:
                self._log(hit, "no_evidence", 0, started, reason=validated.reason_code)
                return EvidenceExtractionResult(
                    relevant=False,
                    evidence_card_ids=[],
                    created_count=0,
                    replayed_count=0,
                    reason_code=validated.reason_code,
                )
            # 5. 全部 items 先完成 quote / schema / draft 校验。
            drafts = self._build_drafts(question, hit, fresh, validated)
            # 6. validation 全部通过后才开始持久化（单 Hit 最多 3 卡）。
            card_ids: list[UUID] = []
            created = 0
            replayed = 0
            for draft in drafts:
                result: EvidenceCardResult = await self._card_service.create_card(draft)
                card_ids.append(result.evidence_card_id)
                if result.replayed:
                    replayed += 1
                else:
                    created += 1
            self._log(hit, "evidence", len(drafts), started)
            return EvidenceExtractionResult(
                relevant=True,
                evidence_card_ids=card_ids,
                created_count=created,
                replayed_count=replayed,
                reason_code=None,
            )
        except EvidenceError as exc:
            self._log_error(hit, exc.code, started)
            raise

    # ------------------------------------------------------------ 内部

    @staticmethod
    def _validate_research_question(research_question: str) -> str:
        if not isinstance(research_question, str) or not research_question.strip():
            raise EvidenceExtractionInputError("research_question 不能为空（trim 后）")
        return research_question.strip()

    @staticmethod
    def _validate_decision(decision) -> EvidenceExtractionDecision:
        """strict schema validation：任意结构不符 → EvidenceExtractionMalformedOutput。"""
        try:
            return EvidenceExtractionDecision.model_validate(decision)
        except (ValidationError, TypeError, ValueError) as exc:
            raise EvidenceExtractionMalformedOutput() from exc

    async def _load_fresh_chunk(self, hit: RetrievalHit) -> _FreshChunk:
        """短 DB read：按 hit.chunk_id 加载当前 chunk + provenance；stale → 抛错。"""
        async with self._sessionmaker() as session:
            chunk = await DocumentChunkRepository(session).get_by_id(hit.chunk_id)
            if chunk is None:
                raise EvidenceExtractionInputStale("chunk 不存在")
            chunk_set = await ChunkSetRepository(session).get_by_id(chunk.chunk_set_id)
            if chunk_set is None:
                raise EvidenceExtractionInputStale("chunk_set 不存在")
            parsed = await ParsedSourceRepository(session).get_by_id(chunk_set.parsed_source_id)
            if parsed is None:
                raise EvidenceExtractionInputStale("parsed_source 不存在")
            record = await SourceRecordRepository(session).get_by_id(parsed.source_id)
            if record is None:
                raise EvidenceExtractionInputStale("source_record 不存在")
            fresh = _FreshChunk(
                chunk_id=chunk.chunk_id,
                chunk_set_id=chunk.chunk_set_id,
                parsed_source_id=chunk_set.parsed_source_id,
                source_id=record.source_id,
                company_id=record.company_id,
                text=chunk.text,
                text_sha256=chunk.text_sha256,
            )
        self._assert_not_stale(hit, fresh)
        return fresh

    @staticmethod
    def _assert_not_stale(hit: RetrievalHit, fresh: _FreshChunk) -> None:
        """RetrievalHit 与当前 PG provenance 一致性校验（纯函数）。

        - sha256(hit.text) == 当前 chunk.text_sha256（hit.text 已 stale）；
        - hit 的 chunk / chunk_set / parsed_source / source / company ids 与
          当前 provenance 一致。
        """
        if hashlib.sha256(hit.text.encode("utf-8")).hexdigest() != fresh.text_sha256:
            raise EvidenceExtractionInputStale("hit.text 与当前 chunk 文本不一致")
        if (
            hit.chunk_id != fresh.chunk_id
            or hit.chunk_set_id != fresh.chunk_set_id
            or hit.parsed_source_id != fresh.parsed_source_id
            or hit.source_id != fresh.source_id
            or hit.company_id != fresh.company_id
        ):
            raise EvidenceExtractionInputStale("hit provenance 与当前 PG 不一致")

    def _build_drafts(
        self,
        question: str,
        hit: RetrievalHit,
        fresh: _FreshChunk,
        decision: EvidenceExtractionDecision,
    ) -> list[EvidenceCardDraft]:
        """全部 items 先完成 quote 解析与 draft 构造（任一失败 → 无卡被创建）。

        quote 以 fresh.text（当前 PG chunk）为准；extractor 身份与置信度由
        item + 冻结常量派生。
        """
        drafts: list[EvidenceCardDraft] = []
        for item in decision.items:
            start, end = resolve_exact_quote(fresh.text, item.quote_text)
            drafts.append(
                EvidenceCardDraft(
                    research_question=question,
                    evidence_statement=item.evidence_statement,
                    evidence_type=item.evidence_type,
                    chunk_id=hit.chunk_id,
                    quote_start=start,
                    quote_end=end,
                    extractor_name=EVIDENCE_EXTRACTOR_NAME,
                    extractor_version=EVIDENCE_EXTRACTOR_VERSION,
                    extractor_model_id=self._model_id,
                    extractor_confidence=item.confidence,
                )
            )
        return drafts

    # ------------------------------------------------------------ 日志（§10：只记白名单字段）

    def _log(
        self,
        hit: RetrievalHit,
        decision: str,
        item_count: int,
        started: float,
        *,
        reason: EvidenceExtractionReason | None = None,
    ) -> None:
        logger.info(
            "evidence_extraction",
            model_id=self._model_id,
            company_id=str(hit.company_id),
            source_id=str(hit.source_id),
            chunk_id=str(hit.chunk_id),
            decision=decision,
            item_count=item_count,
            duration_ms=int((time.monotonic() - started) * 1000),
            reason_code=reason.value if reason is not None else None,
        )

    def _log_error(self, hit: RetrievalHit, error_code: str, started: float) -> None:
        logger.warning(
            "evidence_extraction",
            model_id=self._model_id,
            company_id=str(hit.company_id),
            source_id=str(hit.source_id),
            chunk_id=str(hit.chunk_id),
            decision="error",
            item_count=0,
            duration_ms=int((time.monotonic() - started) * 1000),
            reason_code=None,
            error_code=error_code,
        )
