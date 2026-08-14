"""InsightForge full variant factory (stage 7B.1.4C.4).

把 raw 依赖装配为 `InsightForgeFullVariantRunner`（实现 `VariantRunner` protocol）。
runner 构造时绑定 `EvalExecutionConfig`，运行期校验 config↔spec fingerprint 一致；
per-attempt collection 命名空间在 run 时派生（绑定 execution_id）。

`create_full_model_factory_bundle` 是**生产 model factory bundle**：把 frozen
`config.model` 绑定进 settings，产出 10 个 `create_*` callable（真实生产 DeepSeek
adapters）+ `create_stage4_deps` / `create_stage5_deps`（镜像生产
`create_stage4_dependencies` / `create_stage5_dependencies` 的装配链）。runner 在
run 内用这些 factory 创建 per-attempt 模型（线程 usage_observer）；E2E / 单测用
fake factory bundle 替换，runner 本身不 import 任何 DeepSeek adapter。
"""

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.analysis.claims.factory import create_claim_analysis_model
from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.financial.factory import create_financial_analysis_model
from app.analysis.financial.service import FinancialAnalysisService
from app.analysis.macro.factory import create_macro_analysis_model
from app.analysis.macro.service import MacroAnalysisService
from app.analysis.synthesis.factory import create_synthesis_analysis_model
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.analysis.valuation.factory import create_valuation_analysis_model
from app.analysis.valuation.service import ValuationAnalysisService
from app.audit.service import ReportAuditService
from app.core.config import Settings
from app.draft_section.factory import create_draft_section_model
from app.draft_section.service import DraftSectionService
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import EvalExecutionConfig
from app.eval.remap import StructuredEvidenceRemapService
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.eval.variants.insightforge_full.contracts import (
    FullModelFactoryBundle,
)
from app.eval.variants.insightforge_full.runner import InsightForgeFullVariantRunner
from app.llm.factory import create_evidence_extraction_model
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.contracts import EmbeddingProvider
from app.report.check_service import ReportCheckService
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.research_backflow.service import ResearchBackflowService
from app.research_planning.planner import create_research_planner_model
from app.review.service import ReviewActionService
from app.revision.factory import create_revision_writer_model
from app.revision.service import RevisionService
from app.services.chunking_service import ChunkingService
from app.services.source_parsing_service import SourceParsingService
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage5.dependencies import Stage5WorkflowDependencies
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager


def create_full_model_factory_bundle(
    config: EvalExecutionConfig,
    settings: Settings,
) -> FullModelFactoryBundle:
    """生产 model factory bundle：10 个 firing model + stage4/stage5 deps 全部绑定
    frozen `config.model`（provider / model_id 无硬编码）。

    全部 callable 在 **run 时**被 runner 调用（传入 per-attempt usage_observer）；
    config-bound settings 在此构造一次。装配阶段 0 LLM call；真实 model call 只
    发生在 run 中。
    """
    bound = settings.model_copy(
        update={
            "llm_provider": config.model.provider,
            "llm_model": config.model.model_id,
        }
    )

    def _create_stage4_deps(
        sessionmaker: async_sessionmaker,
        observer: LlmUsageObserver | None,
    ) -> Stage4AnalysisDependencies:
        return Stage4AnalysisDependencies(
            sessionmaker=sessionmaker,
            claim_analysis_service=ClaimAnalysisService(
                sessionmaker, create_claim_analysis_model(bound, usage_observer=observer)
            ),
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
                sessionmaker,
                create_synthesis_analysis_model(bound, usage_observer=observer),
            ),
        )

    def _create_stage5_deps(
        sessionmaker: async_sessionmaker,
        observer: LlmUsageObserver | None,
    ) -> Stage5WorkflowDependencies:
        """镜像生产 `create_stage5_dependencies`（同一断环点）。"""
        from app.audit.adapters import DeepSeekAuditModel

        outline_service = ReportOutlineService(sessionmaker)
        draft_section_service = DraftSectionService(
            sessionmaker, create_draft_section_model(bound, usage_observer=observer)
        )
        report_service = ReportService(sessionmaker, draft_section_service)
        check_service = ReportCheckService(sessionmaker, report_service)
        audit_service = ReportAuditService(
            sessionmaker,
            DeepSeekAuditModel(bound, usage_observer=observer),
            check_service,
        )
        review_action_service = ReviewActionService(sessionmaker, audit_service)
        revision_service = RevisionService(
            sessionmaker,
            model=create_revision_writer_model(bound, usage_observer=observer),
            draft_section_service=draft_section_service,
            check_service=check_service,
            review_action_service=review_action_service,
        )
        report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环
        research_backflow_service = ResearchBackflowService(
            sessionmaker, review_action_service, report_service
        )
        return Stage5WorkflowDependencies(
            sessionmaker=sessionmaker,
            report_outline_service=outline_service,
            draft_section_service=draft_section_service,
            report_service=report_service,
            report_check_service=check_service,
            report_audit_service=audit_service,
            review_action_service=review_action_service,
            revision_service=revision_service,
            research_backflow_service=research_backflow_service,
        )

    return FullModelFactoryBundle(
        provider=config.model.provider,
        model_id=config.model.model_id,
        create_planner=lambda obs: create_research_planner_model(bound, usage_observer=obs),
        create_evidence=lambda obs: create_evidence_extraction_model(bound, usage_observer=obs),
        create_claim=lambda obs: create_claim_analysis_model(bound, usage_observer=obs),
        create_financial=lambda obs: create_financial_analysis_model(bound, usage_observer=obs),
        create_macro=lambda obs: create_macro_analysis_model(bound, usage_observer=obs),
        create_valuation=lambda obs: create_valuation_analysis_model(bound, usage_observer=obs),
        create_synthesis=lambda obs: create_synthesis_analysis_model(bound, usage_observer=obs),
        create_draft=lambda obs: create_draft_section_model(bound, usage_observer=obs),
        create_audit=lambda obs: _create_audit(bound, obs),
        create_revision=lambda obs: create_revision_writer_model(bound, usage_observer=obs),
        create_stage4_deps=_create_stage4_deps,
        create_stage5_deps=_create_stage5_deps,
    )


def _create_audit(bound: Settings, observer: LlmUsageObserver | None):
    from app.audit.adapters import DeepSeekAuditModel

    return DeepSeekAuditModel(bound, usage_observer=observer)


def create_insightforge_full_runner(
    *,
    config: EvalExecutionConfig,
    bundle_loader: EvaluationBundleLoader,
    sessionmaker: async_sessionmaker,
    raw_store: LocalRawArtifactStore,
    chroma: ChromaManager,
    embedding_provider: EmbeddingProvider,
    model_factory_bundle: FullModelFactoryBundle,
    checkpoint_uri: str,
) -> InsightForgeFullVariantRunner:
    """装配 insightforge_full runner（config 构造期绑定；factory bundle 可注入 fake）。

    依赖注入边界：runner 运行期只用 `model_factory_bundle` 的 factory 创建模型，
    不直接构造任何 DeepSeek adapter；`checkpoint_uri` 是隔离 PG 的 LangGraph
    checkpointer 连接（attempt DB）。
    """
    rehydrator = EvaluationReplayRehydrator(sessionmaker, raw_store, bundle_loader)
    parsing_service = SourceParsingService(sessionmaker, raw_store)
    chunking_service = ChunkingService(sessionmaker)
    remap_service = StructuredEvidenceRemapService(sessionmaker, bundle_loader)
    return InsightForgeFullVariantRunner(
        config=config,
        bundle_loader=bundle_loader,
        rehydrator=rehydrator,
        parsing_service=parsing_service,
        chunking_service=chunking_service,
        sessionmaker=sessionmaker,
        embedding_provider=embedding_provider,
        chroma=chroma,
        model_factory_bundle=model_factory_bundle,
        remap_service=remap_service,
        checkpoint_uri=checkpoint_uri,
    )
