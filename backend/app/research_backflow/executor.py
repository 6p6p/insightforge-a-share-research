"""Supplemental research execution (stage 7A.2B.3 spec K-X): 已有 Source Library 检索执行。

`ResearchBackflowExecutor.execute_supplemental_research(verified_request, plan_payload)`：

- **只研究已有 Source Library**：不 live fetch / 不 download / 不 Playwright；
  需要新源由上游 `manual_required(source_acquisition_required)` 承载（不假装完成）；
- 每个 need_spec（确定性流程，**0 LLM query 生成**——query 由 plan 冻结模板派生）：
  1. `_eligible_sources`：company 的 source_records，document_type ∈
     `allowed_source_types`，且遵守 no-lookahead（只研究基准日
     `analysis_as_of` 之前可得的 source）；**无满足 source →
     manual_required(source_acquisition_required)**；
  2. 每条确定性 query → 真实 `RetrievalService.retrieve`（Chroma filtered →
     PG hydrate；**不手工构造 RetrievalHit**）；`RetrievalIndexNotReady` →
     可选 `IndexBuilder.ensure_indexed`（确定性补建，只 archived+parsed）→
     重试；仍失败 → manual_required(index_not_ready)；
  3. 每个 hit → `EvidenceExtractionService.extract_from_hit(source synthesis
     research_question, hit)`（research_question 用 source synthesis 的，
     满足 Gate C）→ `EvidenceCardService.create_card`（fingerprint replay
     幂等）；
  4. created / replayed 全空且无 index 故障 → manual_required(
     evidence_not_extracted)（有 index 但抽取 0 证据）。

汇总 `ResearchBackflowExecutionResult`（**仅 application output**，不保存 model
reasoning / prompt / query 明细）：per-need attempts + 全量 created/replayed
卡 id（canonical 排序）+ `all_manual_required` + `resolved_need_codes`。
executor 不抛确定性错误（manual_required 承载失败语义）；`EvidenceError`
（stale / malformed 等）与 document executor 同语义向调用方传播。
"""

from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.claims.macro_policy import resolve_availability
from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import EvidenceOrigin
from app.evidence.extractor.contracts import EvidenceExtractionModel
from app.evidence.extractor.service import EvidenceExtractionService
from app.rag.retrieval.contracts import RetrievalQuery
from app.rag.retrieval.errors import RetrievalIndexNotReady
from app.rag.retrieval.service import RetrievalService
from app.research_backflow.contracts import (
    RESEARCH_BACKFLOW_MANUAL_REASON_EVIDENCE_NOT_EXTRACTED,
    RESEARCH_BACKFLOW_MANUAL_REASON_INDEX_NOT_READY,
    RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION,
    RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED,
    RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED,
    ResearchBackflowExecutionResult,
    ResearchBackflowNeedExecution,
    VerifiedResearchBackflowRequest,
)
from app.services.evidence_card_service import EvidenceCardService

# 单次检索 top_k（固定，不随业务参数变化；镜像 document executor）。
_TOP_K = 5


def _source_available(source: SourceRecordModel, analysis_as_of: date) -> bool:
    """no-lookahead：只有基准日之前可得的 source 才 eligible。

    镜像 Preparation / document executor；补充研究不研究未来可得 source。
    """
    availability = resolve_availability(
        origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
        snapshot_fetched_at=None,
        source_published_at=source.published_at,
        source_acquired_at=source.acquired_at,
    )
    return availability is not None and availability.date() <= analysis_as_of


class IndexBuilder(Protocol):
    """可选依赖：source 存在但没有 ready index 时确定性补建（archived+parsed）。

    `ensure_indexed` 返回是否已建立 ready index（false = 无法确定性补建）。
    """

    async def ensure_indexed(self, source_id: UUID) -> bool: ...


class _RecordingCardService:
    """包装 EvidenceCardService.create_card，精确记录 created / replayed 卡。"""

    def __init__(self, inner: EvidenceCardService) -> None:
        self._inner = inner
        self.created: list[UUID] = []
        self.existing: list[UUID] = []

    async def create_card(self, draft):
        result = await self._inner.create_card(draft)
        (self.existing if result.replayed else self.created).append(result.evidence_card_id)
        return result


class ResearchBackflowExecutor:
    """已有 Source Library 补充研究执行（spec K-X）：plan need_specs → 检索 → 证据。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        retrieval_service: RetrievalService,
        extractor_model: EvidenceExtractionModel,
        index_builder: IndexBuilder | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._retrieval = retrieval_service
        self._extractor_model = extractor_model
        self._index_builder = index_builder

    # ------------------------------------------------------------ 主入口

    async def execute_supplemental_research(
        self,
        verified_request: VerifiedResearchBackflowRequest,
        plan_payload: dict,
    ) -> ResearchBackflowExecutionResult:
        attempts: list[ResearchBackflowNeedExecution] = []
        new_ids: list[UUID] = []
        replayed_ids: list[UUID] = []
        for spec in plan_payload.get("need_specs", []):
            attempt, created, replayed = await self._execute_need(verified_request, spec)
            attempts.append(attempt)
            new_ids.extend(created)
            replayed_ids.extend(replayed)
        if not attempts:
            return ResearchBackflowExecutionResult(
                attempts=(),
                new_evidence_card_ids=(),
                replayed_evidence_card_ids=(),
                all_manual_required=True,
                resolved_need_codes=(),
            )
        return ResearchBackflowExecutionResult(
            attempts=tuple(attempts),
            new_evidence_card_ids=tuple(sorted(set(new_ids), key=str)),
            replayed_evidence_card_ids=tuple(sorted(set(replayed_ids), key=str)),
            all_manual_required=all(
                a.status == RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED for a in attempts
            ),
            resolved_need_codes=tuple(
                a.need_code for a in attempts if a.status == RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED
            ),
        )

    # ------------------------------------------------------------ 单 need_spec

    async def _execute_need(
        self,
        verified_request: VerifiedResearchBackflowRequest,
        spec: dict,
    ) -> tuple[ResearchBackflowNeedExecution, list[UUID], list[UUID]]:
        need_code = spec["need_code"]
        allowed = sorted(spec.get("allowed_source_types", []))
        sources = await self._eligible_sources(
            verified_request.company_id, allowed, verified_request.analysis_as_of
        )
        if not sources:
            return (
                self._manual(need_code, RESEARCH_BACKFLOW_MANUAL_REASON_SOURCE_ACQUISITION),
                [],
                [],
            )

        recorder = _RecordingCardService(EvidenceCardService(self._sessionmaker))
        not_indexable = False
        source_ids = [s.source_id for s in sources]
        research_question = verified_request.verified_source_synthesis.research_question
        for query_text in spec.get("retrieval_queries", []):
            query = RetrievalQuery(
                company_id=verified_request.company_id,
                query_text=query_text,
                top_k=_TOP_K,
                document_types=allowed,
            )
            try:
                hits = await self._retrieval.retrieve(query)
            except RetrievalIndexNotReady:
                if not await self._ensure_all_indexed(source_ids):
                    not_indexable = True
                    continue
                try:
                    hits = await self._retrieval.retrieve(query)
                except RetrievalIndexNotReady:
                    not_indexable = True
                    continue
            if not hits:
                continue
            extractor = EvidenceExtractionService(
                self._sessionmaker, self._extractor_model, recorder
            )
            for hit in hits:
                await extractor.extract_from_hit(research_question, hit)

        created = list(recorder.created)
        replayed = list(recorder.existing)
        if created or replayed:
            status = RESEARCH_BACKFLOW_NEED_STATUS_RESOLVED
            reason = None
        else:
            status = RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED
            reason = (
                RESEARCH_BACKFLOW_MANUAL_REASON_INDEX_NOT_READY
                if not_indexable
                else RESEARCH_BACKFLOW_MANUAL_REASON_EVIDENCE_NOT_EXTRACTED
            )
        return (
            ResearchBackflowNeedExecution(
                need_code=need_code,
                status=status,
                created_evidence_card_ids=tuple(created),
                replayed_evidence_card_ids=tuple(replayed),
                manual_required_reason=reason,
            ),
            created,
            replayed,
        )

    async def _ensure_all_indexed(self, source_ids: list[UUID]) -> bool:
        if self._index_builder is None:
            return False
        ok = True
        for source_id in source_ids:
            try:
                if not await self._index_builder.ensure_indexed(source_id):
                    ok = False
            except Exception:  # noqa: BLE001 - 补建失败 → 不指数（不泄漏异常）
                ok = False
        return ok

    # ------------------------------------------------------------ sources

    async def _eligible_sources(
        self,
        company_id: UUID,
        allowed: list[str],
        analysis_as_of: date,
    ) -> list[SourceRecordModel]:
        stmt = select(SourceRecordModel).where(
            SourceRecordModel.company_id == company_id,
            SourceRecordModel.document_type.in_(allowed),
        )
        async with self._sessionmaker() as session:
            rows = await session.execute(stmt)
            sources = list(rows.scalars().all())
        return [s for s in sources if _source_available(s, analysis_as_of)]

    @staticmethod
    def _manual(need_code: str, reason: str) -> ResearchBackflowNeedExecution:
        return ResearchBackflowNeedExecution(
            need_code=need_code,
            status=RESEARCH_BACKFLOW_NEED_STATUS_MANUAL_REQUIRED,
            created_evidence_card_ids=(),
            replayed_evidence_card_ids=(),
            manual_required_reason=reason,
        )
