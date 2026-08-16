"""Top-level research orchestration production wiring (7A.2B.2 spec S).

生产装配：`Settings + async_sessionmaker + LangGraphCheckpointManager →
ResearchOrchestrationDependencies / ResearchOrchestrationRunner`。

复用现有 production factories（**构造 0 model call / 0 network / 0 DB 连接**，
所有 model adapter 惰性加载，与 fulfillment / stage4 / stage5 factory 一致）：
- `create_research_fulfillment_service`：plan / router / preparation / fulfillment
  同一批服务实例（spec S：顶层编排节点与 fulfill 共享，保证 plan fingerprint /
  route verify 一致性）；
- `create_stage4_dependencies` + `Stage4WorkflowRunner`：Stage4 exact child；
- `create_stage5_dependencies` + `Stage5WorkflowRunner`：Stage5 exact child +
  execute / resume + checkpoint 投影（7A.2B.2 spec K/L/M）；
- `LangGraphCheckpointManager`：顶层 + child 共用的 PG Checkpointer
  （顶层 `thread_id=orchestration_id`，child `thread_id=run_id`，spec N）。

自动测试不调用本 factory（测试直接构造 deps 并注入 Fake model）；真实调用只
用于生产 / 受控 smoke（0 real DeepSeek 约束对自动测试仍然成立）。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.config import Settings
from app.llm.factory import create_evidence_extraction_model
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.bge import BGEProvider
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.service import RetrievalService
from app.research_backflow.executor import ResearchBackflowExecutor
from app.research_fulfillment.executors import SourceIndexBuilder
from app.research_fulfillment.factory import create_research_fulfillment_service
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import ResearchOrchestrationChildService
from app.services.chunking_service import ChunkingService
from app.stage4.dependencies import create_stage4_dependencies
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.dependencies import create_stage5_dependencies
from app.stage5.runner import Stage5WorkflowRunner
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager


def create_research_orchestration_dependencies(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    checkpoint_manager: LangGraphCheckpointManager,
    usage_observer: LlmUsageObserver | None = None,
) -> ResearchOrchestrationDependencies:
    """按 Settings 装配完整顶层编排依赖（0 model call / 0 network）。

    可选 `usage_observer` 一路向下传给全部 10 个 production LLM adapter
    （planner / evidence extractor / 5 Stage4 / draft / audit / revision），
    生产默认 None。
    """
    fulfillment = create_research_fulfillment_service(
        settings, sessionmaker, usage_observer=usage_observer
    )
    stage4_runner = Stage4WorkflowRunner(
        sessionmaker,
        checkpoint_manager,
        create_stage4_dependencies(settings, sessionmaker, usage_observer=usage_observer),
    )
    stage5_runner = Stage5WorkflowRunner(
        sessionmaker,
        checkpoint_manager,
        create_stage5_dependencies(settings, sessionmaker, usage_observer=usage_observer),
    )
    child_service = ResearchOrchestrationChildService(sessionmaker, stage4_runner, stage5_runner)
    # backflow loop（7A.2B.3）：复用真实检索链 + 确定性 index builder + 同一
    # research_backflow_service（stage5 runner 绑定实例；构造 0 model call /
    # 0 network / 0 DB 连接，与 fulfillment / stage4 / stage5 factory 一致）。
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    from dataclasses import replace

    from app.rag.embedding.contracts import BGE_SMALL_ZH_V1_5

    embedding_spec = (
        replace(BGE_SMALL_ZH_V1_5, local_path=settings.embedding_local_model_path)
        if settings.embedding_local_model_path
        else BGE_SMALL_ZH_V1_5
    )
    embedding = BGEProvider(embedding_spec)
    retrieval = RetrievalService(
        sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma
    )
    # V1.1 P0-2：index builder 注入 parsing service（未 parse 的 source 在
    # fulfill/backflow 检索时自愈补全 parse → chunk → index）。
    from app.services.source_parsing_service import SourceParsingService
    from app.storage.raw_store import LocalRawArtifactStore

    raw_store = LocalRawArtifactStore(
        root=settings.raw_storage_root,
        max_bytes=settings.source_max_file_size_bytes,
        max_json_bytes=settings.macro_max_json_response_bytes,
    )
    index_builder = SourceIndexBuilder(
        sessionmaker,
        ChunkingService(sessionmaker),
        VectorIndexService(sessionmaker=sessionmaker, embedding_provider=embedding, chroma=chroma),
        parsing_service=SourceParsingService(sessionmaker, raw_store),
    )
    backflow_executor = ResearchBackflowExecutor(
        sessionmaker,
        retrieval,
        create_evidence_extraction_model(settings, usage_observer=usage_observer),
        index_builder=index_builder,
    )
    return ResearchOrchestrationDependencies(
        sessionmaker=sessionmaker,
        plan_service=fulfillment.plan_service,
        router=fulfillment.router,
        preparation=fulfillment.preparation,
        fulfillment=fulfillment,
        child_service=child_service,
        stage4_runner=stage4_runner,
        synthesis_service=SynthesisService(sessionmaker),
        stage5_runner=stage5_runner,
        backflow_service=stage5_runner.dependencies.research_backflow_service,
        backflow_executor=backflow_executor,
    )


def create_research_orchestration_runner(
    settings: Settings,
    sessionmaker: async_sessionmaker,
    checkpoint_manager: LangGraphCheckpointManager,
    dependencies: ResearchOrchestrationDependencies | None = None,
) -> ResearchOrchestrationRunner:
    """按 Settings 装配顶层编排 runner（复用 deps；不传则重建）。"""
    deps = dependencies or create_research_orchestration_dependencies(
        settings, sessionmaker, checkpoint_manager
    )
    return ResearchOrchestrationRunner(sessionmaker, checkpoint_manager, deps)
