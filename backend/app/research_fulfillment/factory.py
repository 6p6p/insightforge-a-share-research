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
    plan_service = ResearchPlanningService(
        sessionmaker, planner_model, CompanyIdentityService(sessionmaker)
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
    document_executor = DocumentNeedExecutor(
        sessionmaker,
        retrieval,
        create_evidence_extraction_model(settings, usage_observer=usage_observer),
        index_builder=index_builder,
        auto_acquisition=auto_acquisition,
    )
    # V1.1 closure：宏观有界自动获取（World Bank；确定性 topic→indicator）。
    from app.services.macro_auto_fetch_service import MacroAutoFetchService

    macro_auto_fetch = MacroAutoFetchService(sessionmaker, raw_store)
    return ResearchFulfillmentService(
        sessionmaker,
        plan_service,
        router,
        preparation,
        document_executor=document_executor,
        financial_executor=FinancialNeedExecutor(sessionmaker),
        macro_executor=MacroNeedExecutor(sessionmaker, auto_fetch=macro_auto_fetch),
        valuation_executor=ValuationNeedExecutor(),
    )
