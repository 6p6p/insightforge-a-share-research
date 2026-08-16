"""Context need executor (Final: Research Context Intelligence).

统一处理外部环境研究需求（context_needs）：监管政策 / 地缘贸易 / 行业指标 /
商品市场 / 宏观时序 / 公司 IR / ESG / 投资者交流材料。

- **MACRO_TIMESERIES**：复用 MacroNeedExecutor 的观测解析 + Source Discovery
  （World Bank provider）→ MacroEvidence 卡；
- **文档型 context**（regulatory / geopolitical / industry / commodity /
  company_ir / esg / investor_presentation）：统一发现（news + search/LLM +
  issuer IR provider 链）→ eligible sources（news_article / other /
  issuer_ir_material）→ 检索（topic 进 query）→ 证据抽取（复用
  DocumentNeedExecutor 的 retrieval + extractor）。

**不阻塞编排**：全部失败 → UNRESOLVED + 稳定 error_code（preparation 的
context missing 不阻塞 ready_for_analysis——研究继续，证据缺口在报告/审计
中体现）。executor 不抛确定性错误。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.source_record import SourceRecordModel
from app.evidence.contracts import EvidenceOrigin, MacroEvidenceDraft
from app.rag.retrieval.contracts import RetrievalQuery
from app.research_fulfillment.contracts import (
    FulfillmentAttempt,
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.executors.document import DocumentNeedExecutor
from app.research_fulfillment.executors.macro import MacroNeedExecutor
from app.research_fulfillment.service import FulfillmentContext
from app.research_planning.contracts import ContextNeed, ContextNeedType
from app.research_planning.preparation import MissingResearchNeed
from app.research_planning.router import SourceRouteEntry
from app.services.macro_evidence_service import MacroEvidenceService
from app.services.source_discovery.contracts import SourceDiscoveryRequest
from app.services.source_discovery.service import SourceDiscoveryService

# 文档型 context 的期望来源类型（company IR / ESG / 投资者交流 → 官网 IR；
# 其余 → 新闻 + 搜索）。
_IR_CONTEXT_TYPES = frozenset(
    {
        ContextNeedType.COMPANY_IR.value,
        ContextNeedType.ESG.value,
        ContextNeedType.INVESTOR_PRESENTATION.value,
    }
)

_TOP_K = 10


class ContextNeedExecutor:
    """context need 自动补证据（复用既有发现/检索/抽取/宏观链，0 新 LLM 数字）。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        document_executor: DocumentNeedExecutor,
        macro_executor: MacroNeedExecutor,
        discovery: SourceDiscoveryService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._document_executor = document_executor
        self._macro_executor = macro_executor
        self._discovery = discovery

    # ------------------------------------------------------------ 主入口

    async def fulfill(
        self,
        *,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
    ) -> FulfillmentAttempt:
        ctx_need = next(
            (item for item in context.payload.context_needs if item.need_code == need.need_code),
            None,
        )
        if ctx_need is None:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNSUPPORTED,
                error_code=FulfillmentErrorCode.UNSUPPORTED_NEED,
            )
        if ctx_need.context_type == ContextNeedType.MACRO_TIMESERIES:
            return await self._fulfill_macro(context, need, entry, ctx_need)
        return await self._fulfill_document(context, need, entry, ctx_need)

    # ------------------------------------------------------------ macro timeseries

    async def _fulfill_macro(
        self,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
        ctx_need: ContextNeed,
    ) -> FulfillmentAttempt:
        if entry is None or not entry.provider_keys:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.PROVIDER_UNAVAILABLE,
                error_code=FulfillmentErrorCode.PROVIDER_UNAVAILABLE,
            )
        rows = await self._macro_executor._load_available_observations(
            context, need, ctx_need.topic, ctx_need.geography
        )
        if not rows and self._discovery is not None:
            try:
                outcome = await self._discovery.discover(
                    SourceDiscoveryRequest(
                        company_id=context.company_id,
                        security_code="",
                        need_kind="macro",
                        as_of=context.analysis_as_of,
                        topic=ctx_need.topic,
                        geo=ctx_need.geography,
                    )
                )
            except Exception:  # noqa: BLE001 - 发现失败 → 保持 unresolved
                outcome = None
            if outcome is not None and outcome.acquired:
                rows = await self._macro_executor._load_available_observations(
                    context, need, ctx_need.topic, ctx_need.geography
                )
        if not rows:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.MACRO_DATA_UNAVAILABLE,
            )
        created: list[UUID] = []
        existing: list[UUID] = []
        macro_service = MacroEvidenceService(self._sessionmaker)
        for obs, snapshot, series in rows[:5]:
            statement = (
                f"{snapshot.indicator_name}（{series.geography_code}）在 "
                f"{obs.period} 的观测值为 {obs.value_numeric}"
            )
            try:
                result = await macro_service.create_macro_card(
                    MacroEvidenceDraft(
                        company_id=context.company_id,
                        research_question=context.research_question,
                        macro_observation_id=obs.observation_id,
                        evidence_statement=statement,
                        extractor_name="macro_fulfillment",
                        extractor_version=1,
                    )
                )
            except Exception:  # noqa: BLE001 - 单条失败不阻塞
                continue
            (existing if result.replayed else created).append(result.evidence_card_id)
        if not created and not existing:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.MACRO_EVIDENCE_MISSING,
            )
        return FulfillmentAttempt(
            need_code=need.need_code,
            need_type=need.need_kind,
            route_type=entry.route_type.value,
            status=FulfillmentStatus.RESOLVED,
            created_artifact_ids=created,
            existing_artifact_ids=existing,
        )

    # ------------------------------------------------------------ document context

    async def _fulfill_document(
        self,
        context: FulfillmentContext,
        need: MissingResearchNeed,
        entry: SourceRouteEntry | None,
        ctx_need: ContextNeed,
    ) -> FulfillmentAttempt:
        if entry is None or not entry.provider_keys:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.PROVIDER_UNAVAILABLE,
                error_code=FulfillmentErrorCode.PROVIDER_UNAVAILABLE,
            )
        ir_type = ctx_need.context_type.value in _IR_CONTEXT_TYPES
        source_types = ("issuer_ir_material", "other") if ir_type else ("news_article", "other")
        sources = await self._eligible_sources(context, source_types)
        if not sources and self._discovery is not None:
            await self._discover(context, ctx_need, ir_type)
            sources = await self._eligible_sources(context, source_types)
        if not sources:
            return self._attempt(
                need,
                entry,
                FulfillmentStatus.UNRESOLVED,
                error_code=FulfillmentErrorCode.SOURCE_NOT_FOUND,
            )
        created, existing, not_indexable = await self._document_executor._fulfill_sources(
            context,
            need,
            entry,
            sources,
            build_query=lambda source: self._context_query(context, ctx_need, source),
        )
        if created or existing:
            return FulfillmentAttempt(
                need_code=need.need_code,
                need_type=need.need_kind,
                route_type=entry.route_type.value,
                status=FulfillmentStatus.RESOLVED,
                created_artifact_ids=created,
                existing_artifact_ids=existing,
            )
        return self._attempt(
            need,
            entry,
            FulfillmentStatus.UNRESOLVED,
            error_code=(FulfillmentErrorCode.INDEX_NOT_READY if not_indexable else None),
        )

    # ------------------------------------------------------------ discovery / query

    async def _discover(
        self, context: FulfillmentContext, ctx_need: ContextNeed, ir_type: bool
    ) -> None:
        """统一发现：news（GDELT）+ search（LLM 候选）+ issuer IR provider 链。"""
        try:
            company = await self._load_company(context.company_id)
        except Exception:  # noqa: BLE001
            return
        if company is None:
            return
        request_types = ("issuer_ir_material", "other") if ir_type else ("news_article", "other")
        for source_type in request_types:
            try:
                await self._discovery.discover(
                    SourceDiscoveryRequest(
                        company_id=context.company_id,
                        security_code=company.security_code,
                        need_kind="document",
                        source_type=source_type,
                        as_of=context.analysis_as_of,
                        research_question=context.research_question,
                        topic=ctx_need.topic,
                    )
                )
            except Exception:  # noqa: BLE001 - 单 provider 失败不阻塞
                continue

    @staticmethod
    def _context_query(
        context: FulfillmentContext, ctx_need: ContextNeed, source: SourceRecordModel
    ) -> RetrievalQuery:
        """确定性 query：research_question + context topic（0 LLM 生成 query）。"""
        query_text = f"{context.research_question} {ctx_need.topic}".strip()
        return RetrievalQuery(
            company_id=context.company_id,
            query_text=query_text,
            top_k=_TOP_K,
            source_ids=[source.source_id],
        )

    async def _eligible_sources(
        self, context: FulfillmentContext, source_types: tuple[str, ...]
    ) -> list[SourceRecordModel]:
        """公司 eligible context sources（no-lookahead；跨类型）。"""
        from app.claims.macro_policy import resolve_availability

        stmt = select(SourceRecordModel).where(
            SourceRecordModel.company_id == context.company_id,
            SourceRecordModel.document_type.in_(source_types),
        )
        async with self._sessionmaker() as session:
            rows = await session.execute(stmt)
            candidates = list(rows.scalars().all())
        eligible = []
        for source in candidates:
            availability = resolve_availability(
                origin_type=EvidenceOrigin.DOCUMENT_CHUNK.value,
                snapshot_fetched_at=None,
                source_published_at=source.published_at,
                source_acquired_at=source.acquired_at,
            )
            if availability is not None and availability.date() <= context.analysis_as_of:
                eligible.append(source)
        return eligible

    async def _load_company(self, company_id: UUID):
        from app.db.models.company import CompanyModel

        async with self._sessionmaker() as session:
            return await session.get(CompanyModel, company_id)

    # ------------------------------------------------------------ attempt

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
