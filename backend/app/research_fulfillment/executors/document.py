"""Document/event need executor (stage 7A.2A spec J/K/L): Retrieval → Evidence。

对一条 missing document/event need 自动补证据：
1. **确定性 RetrievalQuery**：query_text = research_question + need purpose/topic
   固定模板（**不使用 LLM 生成 query**，spec J）；company / source_type /
   period / provider 过滤；
2. **只 query 已 ready indexes**：`RetrievalService.retrieve`（真实 PG eligible +
   Chroma + PG hydrate）；`RetrievalIndexNotReady` 表示该 source 无 ready index；
3. **source 存在但没有 ready index** → 可选的 `IndexBuilder.ensure_indexed`
   （确定性 ChunkingService + VectorIndexService，只对 **archived+parsed**
   source 补建，**不 live download / parse**，spec J）；
4. **RetrievalHit 不能直接成为 Evidence**：经 `EvidenceExtractionService.
   extract_from_hit(research_question, hit)`（Stage3 约束：LLM 只做 semantic
   decision + quote_text）→ `EvidenceCardService.create_card`（fingerprint
   replay → 幂等，spec Q）；
5. 重跑 Preparation 由 service 完成。

失败分类（spec P：区分 Source absent vs Source 存在但 Evidence 未抽取）：
- 无匹配 source → SOURCE_NOT_FOUND；
- source 存在但无 ready index 且无法确定性补建 → INDEX_NOT_READY；
- source 有 index 且已检索但抽取 0 证据 → EVIDENCE_NOT_EXTRACTED；
- route 当时无 provider → PROVIDER_UNAVAILABLE（不 fetch）。

确定性测试注入 FakeExtractionModel / FakeEmbeddingProvider + FakeChroma；
真实 LLM extractor 只用于 smoke（spec J/K/L）。executor 不抛确定性错误。
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
from app.repositories.parsed_source_repository import ParsedSourceRepository
from app.research_fulfillment.contracts import (
    FulfillmentAttempt,
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.service import FulfillmentContext
from app.research_planning.contracts import (
    DocumentNeed,
    EventNeed,
    ResearchDocumentNeedType,
)
from app.research_planning.preparation import MissingResearchNeed
from app.research_planning.router import SourceRouteEntry
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService

# 单次检索 top_k（固定，不随 LLM / 业务参数变化）。
_TOP_K = 5


def _source_available(source: SourceRecordModel, analysis_as_of: date) -> bool:
    """no-lookahead：只有基准日之前可得的 source 才 eligible（镜像 Preparation）。"""
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


class SourceIndexBuilder:
    """生产实现：ParsedSource → ChunkingService → VectorIndexService（确定性，0 LLM）。

    只对**已 archived + parsed** 的 source 补建 index；source 没有 parsed
    source（未解析）→ 返回 False（不 live download / parse）。
    """

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        chunking: ChunkingService,
        indexing,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._chunking = chunking
        self._indexing = indexing

    async def ensure_indexed(self, source_id: UUID) -> bool:
        async with self._sessionmaker() as session:
            parsed = await ParsedSourceRepository(session).get_by_source_id(source_id)
        if parsed is None:
            return False
        chunk_result = await self._chunking.chunk_parsed_source(parsed.parsed_source_id)
        result = await self._indexing.index_chunk_set(chunk_result.chunk_set_id)
        return result.status == "ready"


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


class DocumentNeedExecutor:
    """document / event need 自动补证据（Retrieval → EvidenceExtraction → create/replay）。"""

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

    async def fulfill(
        self,
        *,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
    ) -> FulfillmentAttempt:
        if entry is None or not entry.provider_keys:
            # 路由当时无 provider 能服务该 need：不 fetch（spec J）。
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.PROVIDER_UNAVAILABLE,
                error_code=FulfillmentErrorCode.PROVIDER_UNAVAILABLE,
            )
        if need.need_kind == "event":
            return await self._fulfill_event(context, need, entry)
        return await self._fulfill_document(context, need, entry)

    # ------------------------------------------------------------ document

    async def _fulfill_document(
        self,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry,
    ) -> FulfillmentAttempt:
        doc_need = next(
            (item for item in context.payload.document_needs if item.need_code == need.need_code),
            None,
        )
        if doc_need is None:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNSUPPORTED,
                error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
            )
        if doc_need.source_type == ResearchDocumentNeedType.MACRO_DATASET:
            # 宏观数据集文档 = 本系统宏观数据形态，由 macro executor 处理。
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNSUPPORTED,
                error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
            )
        sources = await self._eligible_sources(context, doc_need, entry)
        if not sources:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.SOURCE_NOT_FOUND,
            )
        created, existing, not_indexable = await self._fulfill_sources(
            context,
            need,
            entry,
            sources,
            build_query=lambda source: self._document_query(context, doc_need, source),
        )
        return self._outcome(need, entry, created, existing, not_indexable)

    # ------------------------------------------------------------ event

    async def _fulfill_event(
        self,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry,
    ) -> FulfillmentAttempt:
        ev_need = next(
            (item for item in context.payload.event_needs if item.need_code == need.need_code),
            None,
        )
        if ev_need is None:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNSUPPORTED,
                error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
            )
        sources = await self._eligible_event_sources(context, entry)
        if not sources:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.SOURCE_NOT_FOUND,
            )
        created, existing, not_indexable = await self._fulfill_sources(
            context,
            need,
            entry,
            sources,
            build_query=lambda source: self._event_query(context, ev_need, source),
        )
        return self._outcome(need, entry, created, existing, not_indexable)

    # ------------------------------------------------------------ 共享流程

    async def _fulfill_sources(
        self,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry,
        sources: list[SourceRecordModel],
        *,
        build_query,
    ) -> tuple[list[UUID], list[UUID], bool]:
        """逐 source：检索（无 ready index 则尝试补建）→ 抽取 → 累积 created/existing。

        返回 (created, existing, not_indexable)。created/existing 空且
        not_indexable=False → 有 index 但抽取 0 证据（EVIDENCE_NOT_EXTRACTED）。
        """
        recorder = _RecordingCardService(EvidenceCardService(self._sessionmaker))
        not_indexable = False
        for source in sources:
            query = build_query(source)
            try:
                hits = await self._retrieval.retrieve(query)
            except RetrievalIndexNotReady:
                if not await self._ensure_indexed(source.source_id):
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
                await extractor.extract_from_hit(context.research_question, hit)
        return recorder.created, recorder.existing, not_indexable

    async def _ensure_indexed(self, source_id: UUID) -> bool:
        if self._index_builder is None:
            return False
        try:
            return await self._index_builder.ensure_indexed(source_id)
        except Exception:  # noqa: BLE001 - 补建失败 → 不指数（不泄漏异常）
            return False

    @staticmethod
    def _outcome(
        need: MissingResearchNeed,
        entry: SourceRouteEntry,
        created: list[UUID],
        existing: list[UUID],
        not_indexable: bool,
    ) -> FulfillmentAttempt:
        if created or existing:
            return FulfillmentAttempt(
                need_code=need.need_code,
                need_type=need.need_kind,
                route_type=entry.route_type.value,
                status=FulfillmentStatus.RESOLVED,
                created_artifact_ids=created,
                existing_artifact_ids=existing,
            )
        if not_indexable:
            # Source 存在但无 ready index 且无法确定性补建 → 未抽取。
            return FulfillmentAttempt(
                need_code=need.need_code,
                need_type=need.need_kind,
                route_type=entry.route_type.value,
                status=FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.INDEX_NOT_READY,
            )
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value,
            status=FulfillmentStatus.UNRESOLVED,
            error_code=FulfillmentErrorCode.EVIDENCE_NOT_EXTRACTED,
        )

    # ------------------------------------------------------------ queries

    @staticmethod
    def _document_query(
        context: FulfillmentContext, doc_need: DocumentNeed, source: SourceRecordModel
    ) -> RetrievalQuery:
        """确定性 query 模板（spec J）：research_question + purpose，不用 LLM 生成。"""
        query_text = f"{context.research_question} {doc_need.purpose}".strip()
        kwargs: dict = {"source_ids": [source.source_id]}
        if doc_need.source_type.value != ResearchDocumentNeedType.OTHER.value:
            kwargs["document_types"] = [doc_need.source_type.value]
        if doc_need.period:
            year = int(doc_need.period)
            kwargs["reporting_period_from"] = date(year, 1, 1)
            kwargs["reporting_period_to"] = date(year, 12, 31)
        return RetrievalQuery(
            company_id=context.company_id, query_text=query_text, top_k=_TOP_K, **kwargs
        )

    @staticmethod
    def _event_query(
        context: FulfillmentContext, ev_need: EventNeed, source: SourceRecordModel
    ) -> RetrievalQuery:
        query_text = f"{context.research_question} {ev_need.topic}".strip()
        return RetrievalQuery(
            company_id=context.company_id,
            query_text=query_text,
            top_k=_TOP_K,
            source_ids=[source.source_id],
            document_types=["news_article"],
        )

    # ------------------------------------------------------------ sources

    async def _eligible_sources(
        self,
        context: FulfillmentContext,
        doc_need: DocumentNeed,
        entry: SourceRouteEntry,
    ) -> list[SourceRecordModel]:
        stmt = select(SourceRecordModel).where(SourceRecordModel.company_id == context.company_id)
        if doc_need.source_type.value != ResearchDocumentNeedType.OTHER.value:
            stmt = stmt.where(SourceRecordModel.document_type == doc_need.source_type.value)
        if entry.provider_keys:
            stmt = stmt.where(SourceRecordModel.provider_key.in_(entry.provider_keys))
        async with self._sessionmaker() as session:
            rows = await session.execute(stmt)
            sources = list(rows.scalars().all())
        sources = [s for s in sources if _source_available(s, context.analysis_as_of)]
        if doc_need.period:
            target_year = int(doc_need.period)
            sources = [
                s
                for s in sources
                if s.reporting_period_end is not None and s.reporting_period_end.year == target_year
            ]
        return sources

    async def _eligible_event_sources(
        self,
        context: FulfillmentContext,
        entry: SourceRouteEntry,
    ) -> list[SourceRecordModel]:
        stmt = select(SourceRecordModel).where(
            SourceRecordModel.company_id == context.company_id,
            SourceRecordModel.document_type == "news_article",
        )
        if entry.provider_keys:
            stmt = stmt.where(SourceRecordModel.provider_key.in_(entry.provider_keys))
        async with self._sessionmaker() as session:
            rows = await session.execute(stmt)
            sources = list(rows.scalars().all())
        return [s for s in sources if _source_available(s, context.analysis_as_of)]

    @staticmethod
    def _attempt(
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
        status: FulfillmentStatus,
        *,
        error_code: FulfillmentErrorCode | None = None,
    ) -> FulfillmentAttempt:
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value if entry is not None else "",
            status=status,
            error_code=error_code,
        )
