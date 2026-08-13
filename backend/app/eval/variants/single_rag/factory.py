"""Single RAG variant factory (stage 7B.1.4C.1).

把 raw 依赖装配为 `SingleRagVariantRunner`（实现 `VariantRunner` protocol）。
runner 在构造时绑定 `EvalExecutionConfig`，运行期校验 config↔spec fingerprint
一致。per-(config, case) collection 命名空间在 run 时派生（依赖 case fingerprint），
因此 runner 持有 raw 依赖（sessionmaker / embedding / chroma）在 run 时构造
`VectorIndexService` / `RetrievalService`。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import EvalExecutionConfig
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.eval.variants.single_rag.contracts import SingleRagAnswerModel
from app.eval.variants.single_rag.runner import SingleRagVariantRunner
from app.rag.embedding.contracts import EmbeddingProvider
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager


def create_single_rag_runner(
    *,
    config: EvalExecutionConfig,
    bundle_loader: EvaluationBundleLoader,
    sessionmaker: async_sessionmaker,
    raw_store: LocalRawArtifactStore,
    chroma: ChromaManager,
    embedding_provider: EmbeddingProvider,
    answer_model: SingleRagAnswerModel,
) -> SingleRagVariantRunner:
    """装配 single_rag runner（config 构造期绑定；answer model 可注入 fake）。"""
    rehydrator = EvaluationReplayRehydrator(sessionmaker, raw_store, bundle_loader)
    parsing_service = SourceParsingService(sessionmaker, raw_store)
    chunking_service = ChunkingService(sessionmaker)
    return SingleRagVariantRunner(
        config=config,
        rehydrator=rehydrator,
        parsing_service=parsing_service,
        chunking_service=chunking_service,
        sessionmaker=sessionmaker,
        embedding_provider=embedding_provider,
        chroma=chroma,
        answer_model=answer_model,
    )
