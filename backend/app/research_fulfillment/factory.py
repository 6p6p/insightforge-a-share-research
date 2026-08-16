"""Research fulfillment production wiring (stage 7A.2A spec G).

生产装配：`Settings + async_sessionmaker → ResearchFulfillmentService`。

- **构造阶段 0 网络 / 0 LLM / 0 Chroma / 0 DB 连接**：所有模型 adapter 惰性加载
  （langchain DeepSeek / SentenceTransformer 只在首次调用时 import/load），
  `ChromaManager` 只在首次 `get_client()` 时建连；
- document 路径用**生产默认共享 collection**：`BGEProvider()`（冻结 immutable
  revision）+ `compute_collection_name(BGE spec)` —— 与 VectorIndexService /
  RetrievalService 天然同名（同一 collection，所有公司 / ChunkSet 共享）；
- document executor 注入真实 `SourceIndexBuilder`（archived+parsed → Chunking →
  VectorIndex，**不 live download / fetch**），因此 no-index 路径在生产自动补建。

自动测试不调用本 factory（测试直接构造 executor 并注入 Fake 模型）；真实调用只
用于受控 smoke / 生产路径（0 real DeepSeek 约束对自动测试仍然成立）。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.llm.factory import create_evidence_extraction_model
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.bge import BGEProvider
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.service import RetrievalService
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    SourceIndexBuilder,
    ValuationNeedExecutor,
)
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_planning.intent import (
    DefaultResearchIntentGenerator,
    create_intent_enhancement_model,
)
from app.research_planning.planner import create_research_planner_model
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.chunking_service import ChunkingService
from app.services.company_identity_service import CompanyIdentityService
from app.vectorstore.client import ChromaManager


def create_research_fulfillment_service(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    usage_observer: LlmUsageObserver | None = None,
) -> ResearchFulfillmentService:
    """按 Settings 装配完整生产 fulfillment（真实 DeepSeek planner/extractor、
    真实 BGE embedding、真实 Chroma 共享 collection）。

    构造阶段不发起任何外部调用；所有模型 adapter 惰性加载。
    可选 `usage_observer` 一路向下传给 planner + evidence extractor。
    """
    planner_model = create_research_planner_model(settings, usage_observer=usage_observer)
    # P0 Company Only Research Flow：无 research_question 的任务由默认研究意图
    # 生成器派生（template 主路径；LLM enhancement 由 intent_llm_enhancement
    # 开关控制，失败降级 template）。构造 0 model call / 0 network。
    intent_generator = DefaultResearchIntentGenerator(
        enhancement_model=create_intent_enhancement_model(settings, usage_observer=usage_observer)
    )
    plan_service = ResearchPlanningService(
        sessionmaker,
        planner_model,
        CompanyIdentityService(sessionmaker),
        intent_generator=intent_generator,
    )
    router = ResearchSourceRouter(sessionmaker, plan_service)
    preparation = ResearchPreparationService(sessionmaker, plan_service, router)

    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    embedding = BGEProvider()
    retrieval = RetrievalService(
        sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma
    )
    from app.services.source_parsing_service import SourceParsingService
    from app.storage.raw_store import LocalRawArtifactStore

    raw_store = LocalRawArtifactStore(
        root=settings.raw_storage_root,
        max_bytes=settings.source_max_file_size_bytes,
        max_json_bytes=settings.macro_max_json_response_bytes,
    )
    parsing_service = SourceParsingService(sessionmaker, raw_store)
    index_builder = SourceIndexBuilder(
        sessionmaker,
        ChunkingService(sessionmaker),
        VectorIndexService(sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma),
        parsing_service=parsing_service,
    )
    # V1.1 closure：受控公告自动发现（East Money Tier-3 后备）——无 eligible
    # source 时自动获取年度/半年度/季度报告并落库（构造 0 network）。
    from app.services.announcement_discovery_service import AnnouncementDiscoveryService

    auto_acquisition = AnnouncementDiscoveryService(
        sessionmaker=sessionmaker,
        raw_store=raw_store,
    )
    # V1.1 closure：宏观有界自动获取（World Bank；确定性 topic→indicator）。
    from app.services.macro_auto_fetch_service import MacroAutoFetchService

    macro_auto_fetch = MacroAutoFetchService(sessionmaker, raw_store)
    # P1 Source Discovery Layer：统一发现入口（provider 链：announcement →
    # macro → search（P2 LLM，settings 开关）→ news（P4 扩展点））。executor
    # 优先走统一层，全部 provider exhausted 才 human fallback（构造 0 network）。
    from app.services.source_discovery import SourceDiscoveryService
    from app.services.source_discovery.providers import (
        AnnouncementDiscoveryProvider,
        MacroDiscoveryProvider,
        SearchDiscoveryProvider,
    )
    from app.services.source_discovery.search_model import create_search_query_model

    # P2：Model Assisted Discovery（LLM 候选发现）——由
    # search_discovery_llm_enabled 开关控制（默认关闭）。候选 URL 仍须经
    # provider 域名 allowlist + SafeFetcher 验证后才落库（不 bypass provenance）。
    search_provider = SearchDiscoveryProvider(
        create_search_query_model(settings, usage_observer=usage_observer),
        sessionmaker=sessionmaker,
        raw_store=raw_store,
        max_bytes=settings.source_max_file_size_bytes,
    )
    # P4：News Discovery（GDELT 候选 → 原创发布者验证链）——由
    # news_discovery_enabled 开关控制（默认关闭，真实网络调用需显式启用）。
    from app.services.source_discovery.providers import NewsDiscoveryProvider

    news_provider = NewsDiscoveryProvider(
        sessionmaker=sessionmaker,
        raw_store=raw_store,
        enabled=settings.news_discovery_enabled,
    )
    discovery = SourceDiscoveryService(
        [
            AnnouncementDiscoveryProvider(auto_acquisition),
            MacroDiscoveryProvider(macro_auto_fetch),
            search_provider,
            news_provider,
        ]
    )
    document_executor = DocumentNeedExecutor(
        sessionmaker,
        retrieval,
        create_evidence_extraction_model(settings, usage_observer=usage_observer),
        index_builder=index_builder,
        auto_acquisition=auto_acquisition,
        discovery=discovery,
    )
    # F1 Financial Intelligence：真实自动财务提取链（deterministic，0 LLM）——
    # 年报 parsed blocks → 指标候选 → numeric provenance 校验 → 证据卡 +
    # observation 落库。FinancialNeedExecutor 缺 observation 时自动触发。
    from app.financial.extraction.evidence import FinancialExtractionEvidenceService
    from app.financial.extraction.ingestion import FinancialExtractionIngestionService
    from app.financial.extraction.service import FinancialExtractionService
    from app.financial.extraction.statement_provider import StatementLineExtractionProvider

    statement_provider = StatementLineExtractionProvider(sessionmaker)
    financial_extraction_service = FinancialExtractionService(sessionmaker, statement_provider)
    financial_ingestion = FinancialExtractionIngestionService(
        sessionmaker,
        FinancialExtractionEvidenceService(sessionmaker),
    )
    return ResearchFulfillmentService(
        sessionmaker,
        plan_service,
        router,
        preparation,
        document_executor=document_executor,
        financial_executor=FinancialNeedExecutor(
            sessionmaker,
            extraction=financial_ingestion,
            extraction_service=financial_extraction_service,
            provider=statement_provider,
        ),
        macro_executor=MacroNeedExecutor(
            sessionmaker, auto_fetch=macro_auto_fetch, discovery=discovery
        ),
        valuation_executor=ValuationNeedExecutor(),
    )
