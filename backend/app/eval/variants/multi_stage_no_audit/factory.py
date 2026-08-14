"""Multi-stage no-audit variant factory (stage 7B.1.4C.2).

把 raw 依赖装配为 `MultiStageNoAuditVariantRunner`（实现 `VariantRunner` protocol）。
runner 构造时绑定 `EvalExecutionConfig`，运行期校验 config↔spec fingerprint 一致；
per-attempt collection 命名空间在 run 时派生（绑定 execution_id）。

`create_multi_stage_model_factory_bundle` 是**生产 model factory bundle**：把 frozen
`config.model` 绑定进 settings，产出 5 个 `create_*` callable（真实生产 DeepSeek
adapters）+ `create_stage4_deps`。runner 在 run 内用这些 factory 创建 per-attempt
模型（线程 usage_observer）；E2E / 单测用 fake factory bundle 替换，runner 本身
不 import 任何 DeepSeek adapter。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.contracts import ClaimAnalysisModel
from app.analysis.claims.factory import create_claim_analysis_model
from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.financial.factory import create_financial_analysis_model
from app.analysis.financial.service import FinancialAnalysisService
from app.analysis.macro.factory import create_macro_analysis_model
from app.analysis.macro.service import MacroAnalysisService
from app.analysis.synthesis.factory import create_synthesis_analysis_model
from app.analysis.synthesis.model import SynthesisAnalysisModel
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.analysis.valuation.factory import create_valuation_analysis_model
from app.analysis.valuation.service import ValuationAnalysisService
from app.core.config import Settings
from app.draft_section.factory import create_draft_section_model
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import EvalExecutionConfig
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.eval.variants.multi_stage_no_audit.contracts import (
    MultiStageModelFactoryBundle,
)
from app.eval.variants.multi_stage_no_audit.runner import MultiStageNoAuditVariantRunner
from app.llm.factory import create_evidence_extraction_model
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.contracts import EmbeddingProvider
from app.research_planning.planner import create_research_planner_model
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager


def create_multi_stage_model_factory_bundle(
    config: EvalExecutionConfig,
    settings: Settings,
) -> MultiStageModelFactoryBundle:
    """生产 model factory bundle：5 个 firing model + stage4 deps 全部绑定 frozen
    `config.model`（provider / model_id 无硬编码）。

    全部 callable 在 **run 时**被 runner 调用（传入 per-attempt usage_observer）；
    config-bound settings 在此构造一次。装配阶段 0 LLM call；真实 model call 只
    发生在 run 中。`create_stage4_deps` 复用本 bundle 的 create_claim /
    create_synthesis（fake 可传播进 Stage4），另以生产 adapter 构造 document-only
    下从不 dispatch 的 financial / macro / valuation analysis services。
    """
    bound = settings.model_copy(
        update={
            "llm_provider": config.model.provider,
            "llm_model": config.model.model_id,
        }
    )

    def _create_claim(observer: LlmUsageObserver | None) -> ClaimAnalysisModel:
        return create_claim_analysis_model(bound, usage_observer=observer)

    def _create_synthesis(observer: LlmUsageObserver | None) -> SynthesisAnalysisModel:
        return create_synthesis_analysis_model(bound, usage_observer=observer)

    def _create_stage4_deps(
        sessionmaker: async_sessionmaker,
        observer: LlmUsageObserver | None,
    ) -> Stage4AnalysisDependencies:
        return Stage4AnalysisDependencies(
            sessionmaker=sessionmaker,
            claim_analysis_service=ClaimAnalysisService(sessionmaker, _create_claim(observer)),
            financial_analysis_service=FinancialAnalysisService(
                sessionmaker,
                create_financial_analysis_model(bound, usage_observer=observer),
            ),
            macro_analysis_service=MacroAnalysisService(
                sessionmaker,
                create_macro_analysis_model(bound, usage_observer=observer),
            ),
            valuation_analysis_service=ValuationAnalysisService(
                sessionmaker,
                create_valuation_analysis_model(bound, usage_observer=observer),
            ),
            synthesis_service=SynthesisService(sessionmaker),
            synthesis_analysis_service=SynthesisAnalysisService(
                sessionmaker, _create_synthesis(observer)
            ),
        )

    return MultiStageModelFactoryBundle(
        provider=config.model.provider,
        model_id=config.model.model_id,
        create_planner=lambda obs: create_research_planner_model(bound, usage_observer=obs),
        create_evidence=lambda obs: create_evidence_extraction_model(bound, usage_observer=obs),
        create_claim=_create_claim,
        create_synthesis=_create_synthesis,
        create_draft=lambda obs: create_draft_section_model(bound, usage_observer=obs),
        create_stage4_deps=_create_stage4_deps,
    )


def create_multi_stage_no_audit_runner(
    *,
    config: EvalExecutionConfig,
    bundle_loader: EvaluationBundleLoader,
    sessionmaker: async_sessionmaker,
    raw_store: LocalRawArtifactStore,
    chroma: ChromaManager,
    embedding_provider: EmbeddingProvider,
    model_factory_bundle: MultiStageModelFactoryBundle,
) -> MultiStageNoAuditVariantRunner:
    """装配 multi_stage_no_audit runner（config 构造期绑定；factory bundle 可注入 fake）。

    依赖注入边界：runner 运行期只用 `model_factory_bundle` 的 factory 创建模型，
    不直接构造任何 DeepSeek adapter。
    """
    rehydrator = EvaluationReplayRehydrator(sessionmaker, raw_store, bundle_loader)
    parsing_service = SourceParsingService(sessionmaker, raw_store)
    chunking_service = ChunkingService(sessionmaker)
    return MultiStageNoAuditVariantRunner(
        config=config,
        rehydrator=rehydrator,
        parsing_service=parsing_service,
        chunking_service=chunking_service,
        sessionmaker=sessionmaker,
        embedding_provider=embedding_provider,
        chroma=chroma,
        model_factory_bundle=model_factory_bundle,
    )
