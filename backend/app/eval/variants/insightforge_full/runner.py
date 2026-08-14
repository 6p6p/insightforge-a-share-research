"""InsightForge full variant runner (stage 7B.1.4C.4).

Frozen Bundle → isolated replay → parse/chunk/index → **production top-level
research orchestration**（planner → router → fulfillment → evidence → Stage4 →
synthesis → Stage5: deterministic checks → **semantic Audit → Review Routing →
Revision → Research Backflow**，由真实 workflow 判断）→ final Report →
确定性归一化 `EvalVariantOutput`。

复用（mandatory，不复制逻辑）：
- `ResearchOrchestrationRunner` + 顶层生产 graph（child Stage4/Stage5 用真实
  runner + PG Checkpointer；backflow loop 由 graph 内部驱动）；
- `ResearchFulfillmentService`（document/financial/macro/valuation executors）；
- `StructuredEvidenceRemapService`（frozen structured artifact → attempt 新
  EvidenceCard；见 `app.eval.remap`）；
- Stage4 / Stage5 生产 graph、全部 production services / repositories。

Human Review evaluation policy：`wait_human` interrupt 保留；路由到 human_review
时用确定性 policy 自动 `approve`（`finalize_on_approve` 仍强制 Check=pass，
spec R——人工裁决不能覆盖 Gate 0）；`waiting_manual`（frozen 输入不满足计划）
→ 稳定 fail-fast `insightforge_full_input_not_supported`（不伪造 readiness）。

隔离不变量：
- rehydration 只落在隔离 target PG + store；derived index 用 per-attempt
  collection（绑定 `EvalVariantRuntimeContext.execution_id`）；
- structured remap 只消费本 attempt 的 EvidenceCard；
- 归一化只读本 attempt 的最终 Stage4 claim_ids + 最终 Report 行。

模型注入（assembly gate）：`FullModelFactoryBundle` 是唯一模型构造入口——runner
在任何 factory call 前校验 bundle.provider / model_id == frozen config.model
（不一致 → `EvalExecutionAssemblyError`，0 factory call）。**runner 不 import
任何生产 adapter**。预期 usage 组件 = 全部 10 个 production component（audit /
revision_writer 在真实执行时出现；revision_writer 只在 audit 判定 rewrite /
human rewrite 时出现）。
"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.report import ReportModel
from app.db.models.research_task import ResearchTaskModel
from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.canonical import canonical_json_str
from app.eval.contracts import (
    EvalCitation,
    EvalClaim,
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalVariantOutput,
)
from app.eval.errors import (
    EvalExecutionAssemblyError,
    EvalInsightForgeFullInputError,
    EvalOutputStructureError,
    EvalVariantError,
)
from app.eval.execution.contracts import EvalVariantRuntimeContext
from app.eval.fingerprints import compute_execution_config_fingerprint
from app.eval.remap import StructuredEvidenceRemapService
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.eval.variants import EvalVariantId
from app.eval.variants.insightforge_full.contracts import (
    CITATION_KEY_PREFIX,
    EVAL_HUMAN_DECISION,
    INSIGHTFORGE_FULL_CLAIM_TYPE,
    INSIGHTFORGE_FULL_PROMPT_VERSION,
    MAX_EVAL_HUMAN_ROUNDS,
    FullModelFactoryBundle,
)
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.contracts import BGE_SMALL_ZH_V1_5, EmbeddingProvider
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.service import RetrievalService
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_backflow.executor import ResearchBackflowExecutor
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    SourceIndexBuilder,
    ValuationNeedExecutor,
)
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import (
    ResearchOrchestrationChildService,
    ResearchOrchestrationService,
)
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.chunking_service import ChunkingService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_parsing_service import SourceParsingService
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager

_STAGE5_GRAPH_NAME = "stage5_report"
_STAGE4_GRAPH_NAME = "stage4_analysis"


class InsightForgeFullVariantRunner:
    variant_id = EvalVariantId.INSIGHTFORGE_FULL

    def __init__(
        self,
        *,
        config: EvalExecutionConfig,
        bundle_loader,
        rehydrator: EvaluationReplayRehydrator,
        parsing_service: SourceParsingService,
        chunking_service: ChunkingService,
        sessionmaker: async_sessionmaker,
        embedding_provider: EmbeddingProvider,
        chroma: ChromaManager,
        model_factory_bundle: FullModelFactoryBundle,
        remap_service: StructuredEvidenceRemapService,
        checkpoint_uri: str,
    ) -> None:
        if config.variant_id != self.variant_id:
            raise EvalExecutionAssemblyError("config.variant_id 不是 insightforge_full")
        if config.prompt_version != INSIGHTFORGE_FULL_PROMPT_VERSION:
            raise EvalExecutionAssemblyError(
                f"config.prompt_version {config.prompt_version!r} != "
                f"{INSIGHTFORGE_FULL_PROMPT_VERSION!r}"
            )
        self._config = config
        self._loader = bundle_loader
        self._rehydrator = rehydrator
        self._parsing = parsing_service
        self._chunking = chunking_service
        self._sessionmaker = sessionmaker
        self._embedding = embedding_provider
        self._chroma = chroma
        self._bundle = model_factory_bundle
        self._remap = remap_service
        self._checkpoint_uri = checkpoint_uri

    async def run(
        self,
        execution_case: LoadedEvalExecutionCase,
        execution_spec: EvalExecutionSpec,
        *,
        runtime_context: EvalVariantRuntimeContext,
        usage_observer: LlmUsageObserver | None,
    ) -> EvalVariantOutput:
        # 1. config ↔ spec 绑定（assembly，0 model call）。
        self._validate_spec(execution_spec)
        # 2. 模型 / embedding 身份 preflight（0 model call）。
        self._validate_model_identity()
        self._validate_embedding_identity()
        # 3. input closure（>=1 document；macro / structured 可选）。
        self._validate_input(execution_case)

        # 4. rehydrate：frozen bundle → 隔离 PG + store（document + macro）。
        rehydrated = await self._rehydrator.rehydrate_case(
            execution_case.case_id, execution_case.case_version
        )
        if not rehydrated.documents:
            raise EvalInsightForgeFullInputError("rehydration 未产出任何 document source")

        # 5. per-attempt collection 命名空间 + manifest runtime_scope（绑定 execution_id）。
        collection_name = self._collection_name(runtime_context.execution_id)
        runtime_scope = f"eval:insightforge_full:{runtime_context.execution_id.hex}"
        index_service = VectorIndexService(
            self._sessionmaker,
            self._embedding,
            self._chroma,
            collection_name=collection_name,
            runtime_scope=runtime_scope,
        )
        retrieval_service = RetrievalService(
            self._sessionmaker, self._embedding, self._chroma, collection_name=collection_name
        )

        # 6. parse → chunk → index（真实 deterministic pipeline，幂等 create-or-get）。
        for doc in rehydrated.documents:
            parsed = await self._parsing.parse_source(doc.source_record_id)
            chunked = await self._chunking.chunk_parsed_source(parsed.parsed_source_id)
            await index_service.index_chunk_set(chunked.chunk_set_id, force_rebuild=True)

        # 7. per-attempt 模型构造（只用 bundle factory；不 import 生产 adapter）。
        planner_model = self._bundle.create_planner(usage_observer)
        evidence_model = self._bundle.create_evidence(usage_observer)
        deps4 = self._bundle.create_stage4_deps(self._sessionmaker, usage_observer)
        deps5 = self._bundle.create_stage5_deps(self._sessionmaker, usage_observer)

        # 8. 隔离 PG 内创建 ResearchTask（orchestration / planner 都 keyed off task）。
        task_id = await self._create_research_task(execution_case)

        # 9. checkpoint manager（attempt PG；顶层 + child 共用）。
        checkpoint_manager = LangGraphCheckpointManager(self._checkpoint_uri)
        await checkpoint_manager.setup()
        try:
            # 10. 装配顶层编排（真实 services + 生产 graph；fulfillment 包 remap）。
            orchestration_deps = self._assemble_orchestration_deps(
                sessionmaker=self._sessionmaker,
                planner_model=planner_model,
                evidence_model=evidence_model,
                retrieval_service=retrieval_service,
                index_service=index_service,
                deps4=deps4,
                deps5=deps5,
                checkpoint_manager=checkpoint_manager,
                execution_case=execution_case,
            )
            orchestration_runner = ResearchOrchestrationRunner(
                self._sessionmaker, checkpoint_manager, orchestration_deps
            )
            service = ResearchOrchestrationService(
                self._sessionmaker,
                orchestration_deps.plan_service,
                stage5_runner=orchestration_deps.stage5_runner,
                orchestration_runner=orchestration_runner,
            )

            # 11. create orchestration（内部 create plan + orchestration row）→ 执行。
            outcome = await service.create_or_get_orchestration(task_id)
            final_state = await orchestration_runner.run_orchestration(outcome.orchestration_id)

            # 12. evaluation human policy loop（不跳过 Audit / Revision）。
            final_state = await self._drive_to_terminal(
                service, orchestration_runner, outcome.orchestration_id, final_state
            )

            # 13. 确定性归一化（无额外 LLM；复用已装配的 child runners 读 checkpoint）。
            return await self._normalize(
                execution_case,
                rehydrated,
                task_id=task_id,
                stage4_runner=orchestration_deps.stage4_runner,
                stage5_runner=orchestration_deps.stage5_runner,
            )
        finally:
            await checkpoint_manager.close()

    # ------------------------------------------------------------------ 内部

    def _validate_spec(self, execution_spec: EvalExecutionSpec) -> None:
        if execution_spec.variant_id != self.variant_id:
            raise EvalExecutionAssemblyError("execution_spec.variant_id 不是 insightforge_full")
        if (
            compute_execution_config_fingerprint(self._config)
            != execution_spec.execution_config_fingerprint
        ):
            raise EvalExecutionAssemblyError(
                "execution_config_fingerprint 与 runner 绑定 config 不一致"
            )

    def _validate_model_identity(self) -> None:
        config_model = self._config.model
        if self._bundle.provider != config_model.provider:
            raise EvalExecutionAssemblyError(
                f"model_bundle.provider {self._bundle.provider!r} != "
                f"config {config_model.provider!r}"
            )
        if self._bundle.model_id != config_model.model_id:
            raise EvalExecutionAssemblyError(
                f"model_bundle.model_id {self._bundle.model_id!r} != "
                f"config {config_model.model_id!r}"
            )

    def _validate_embedding_identity(self) -> None:
        spec = self._embedding.model_info
        frozen = BGE_SMALL_ZH_V1_5
        if spec.model_id != frozen.model_id:
            raise EvalExecutionAssemblyError(
                f"embedding model_id {spec.model_id!r} != frozen {frozen.model_id!r}"
            )
        if spec.revision is None:
            raise EvalExecutionAssemblyError(
                "embedding model 未配置 immutable revision（revision is None）"
            )
        if spec.revision != frozen.revision:
            raise EvalExecutionAssemblyError(
                f"embedding revision {spec.revision!r} != frozen {frozen.revision!r}"
            )

    @staticmethod
    def _validate_input(execution_case: LoadedEvalExecutionCase) -> None:
        snapshot = execution_case.snapshot
        if not snapshot.document_sources:
            raise EvalInsightForgeFullInputError("insightforge_full 需要 >=1 条 document source")
        # macro / structured 可选；planner 只声明 snapshot 能满足的 need（fake /
        # 生产 planner 均按此约束输出），不足时 fulfillment 以 waiting_manual
        # 表现 → `_drive_to_terminal` 稳定 fail-fast。

    @staticmethod
    def _collection_name(execution_id: UUID) -> str:
        return f"eval_insightforge_full_{execution_id.hex}"

    async def _create_research_task(self, execution_case: LoadedEvalExecutionCase) -> UUID:
        company = execution_case.company
        analysis_as_of = execution_case.analysis_as_of.date()
        task = ResearchTaskModel(
            company_query=f"{company.exchange}:{company.security_code}",
            research_start_date=analysis_as_of,
            research_end_date=analysis_as_of,
            modules=[],
            questions=[execution_case.research_question],
        )
        async with self._sessionmaker() as session:
            created = await ResearchTaskRepository(session).create(task)
            await session.commit()
            return created.task_id

    def _assemble_orchestration_deps(
        self,
        *,
        sessionmaker: async_sessionmaker,
        planner_model,
        evidence_model,
        retrieval_service: RetrievalService,
        index_service: VectorIndexService,
        deps4,
        deps5,
        checkpoint_manager: LangGraphCheckpointManager,
        execution_case: LoadedEvalExecutionCase,
    ) -> ResearchOrchestrationDependencies:
        plan_service = ResearchPlanningService(
            sessionmaker, planner_model, CompanyIdentityService(sessionmaker)
        )
        router = ResearchSourceRouter(sessionmaker, plan_service)
        preparation = ResearchPreparationService(sessionmaker, plan_service, router)
        index_builder = SourceIndexBuilder(sessionmaker, self._chunking, index_service)
        document_executor = DocumentNeedExecutor(
            sessionmaker, retrieval_service, evidence_model, index_builder=index_builder
        )
        fulfillment_inner = ResearchFulfillmentService(
            sessionmaker,
            plan_service,
            router,
            preparation,
            document_executor=document_executor,
            financial_executor=FinancialNeedExecutor(sessionmaker),
            macro_executor=MacroNeedExecutor(sessionmaker),
            valuation_executor=ValuationNeedExecutor(),
        )
        # remap-aware fulfillment：document 证据生成后再 remap structured artifacts，
        # 第二轮 fulfill 解析 financial / valuation needs（幂等 replay，安全）。
        fulfillment = _RemapAwareFulfillment(fulfillment_inner, self._remap, execution_case)
        stage4_runner = Stage4WorkflowRunner(sessionmaker, checkpoint_manager, deps4)
        stage5_runner = Stage5WorkflowRunner(sessionmaker, checkpoint_manager, deps5)
        child_service = ResearchOrchestrationChildService(
            sessionmaker, stage4_runner, stage5_runner=stage5_runner
        )
        backflow_executor = ResearchBackflowExecutor(
            sessionmaker,
            retrieval_service,
            evidence_model,
            index_builder=index_builder,
        )
        return ResearchOrchestrationDependencies(
            sessionmaker=sessionmaker,
            plan_service=plan_service,
            router=router,
            preparation=preparation,
            fulfillment=fulfillment,
            child_service=child_service,
            stage4_runner=stage4_runner,
            synthesis_service=SynthesisService(sessionmaker),
            stage5_runner=stage5_runner,
            backflow_service=stage5_runner.dependencies.research_backflow_service,
            backflow_executor=backflow_executor,
        )

    async def _drive_to_terminal(
        self,
        service: ResearchOrchestrationService,
        orchestration_runner: ResearchOrchestrationRunner,
        orchestration_id: UUID,
        final_state: dict,
    ) -> dict:
        """驱动 orchestration 到 terminal（completed / failed / 稳定 fail-fast）。

        Evaluation human policy：`awaiting_stage5` → 自动 `approve`
        （`act_on_orchestration` 内部 resume Stage5 child + 继续顶层；approve 后
        `finalize_on_approve` 仍强制 Check=pass）。`waiting_manual` /
        research_backflow manual → frozen 输入不满足计划 → 稳定 fail-fast
        （不伪造 readiness、不假装 research completed）。
        """
        from app.research_orchestration.contracts import OrchestrationPhase, OrchestrationStatus

        state = dict(final_state)
        for _ in range(MAX_EVAL_HUMAN_ROUNDS + 1):
            row = await self._orchestration_row(orchestration_id)
            status = row["status"]
            phase = state.get("current_phase") or row["current_phase"]
            if status == OrchestrationStatus.COMPLETED.value:
                return state
            if status == OrchestrationStatus.FAILED.value:
                raise EvalVariantError(
                    f"insightforge_full orchestration failed (error_code={row['error_code']})"
                )
            if status == OrchestrationStatus.WAITING_HUMAN.value:
                if phase == OrchestrationPhase.AWAITING_STAGE5.value:
                    # Evaluation policy：自动 approve（Check=pass 由生产节点强制）。
                    # `act_on_orchestration` 内部 resume Stage5 child + 继续顶层
                    # graph 到 terminal（或再次 pause）——不重复调用 run_orchestration。
                    await service.act_on_orchestration(
                        orchestration_id, EVAL_HUMAN_DECISION, comment="eval-auto-approve"
                    )
                    state = await self._orchestration_state(orchestration_id)
                    continue
                if phase == OrchestrationPhase.WAITING_MANUAL.value:
                    raise EvalInsightForgeFullInputError(
                        "frozen snapshot 无法满足 research plan（waiting_manual）"
                    )
                if phase == OrchestrationPhase.RESEARCH_BACKFLOW.value:
                    raise EvalInsightForgeFullInputError(
                        "research backflow 需要人工补资料（frozen snapshot 无法满足）"
                    )
                raise EvalVariantError(f"insightforge_full 停在未知 waiting_human phase: {phase}")
            raise EvalVariantError(
                f"insightforge_full orchestration 未达 terminal（status={status}, phase={phase}）"
            )
        raise EvalVariantError("insightforge_full human-review 自动裁决超限")

    async def _orchestration_state(self, orchestration_id: UUID) -> dict:
        """从 orchestration row 读取当前 phase（act 之后不再跑 graph 时的状态来源）。"""
        from app.research_orchestration.contracts import OrchestrationPhase

        row = await self._orchestration_row(orchestration_id)
        state: dict = {}
        if row["current_phase"] is not None:
            state["current_phase"] = row["current_phase"]
        # 兼容：orchestration 已 completed 但 row 的 phase 可能未同步——从
        # checkpoint 派生 phase。
        if row["status"] == "completed":
            state["current_phase"] = OrchestrationPhase.COMPLETED.value
        return state

    async def _orchestration_row(self, orchestration_id: UUID) -> dict:
        from app.db.models.research_orchestration import ResearchOrchestrationModel

        async with self._sessionmaker() as session:
            orchestration = await session.get(ResearchOrchestrationModel, orchestration_id)
        if orchestration is None:
            raise EvalVariantError("orchestration row missing")
        return {
            "status": orchestration.status,
            "current_phase": orchestration.current_phase,
            "error_code": orchestration.error_code,
        }

    # ------------------------------------------------------------ 归一化

    async def _normalize(
        self,
        execution_case: LoadedEvalExecutionCase,
        rehydrated,
        *,
        task_id: UUID,
        stage4_runner,
        stage5_runner,
    ) -> EvalVariantOutput:
        """确定性归一化：最终 Stage4 claim_ids + 最终 Report → `EvalVariantOutput`.

        citation 链 = EvidenceCard → SourceRecord → FrozenDocumentSourceRef →
        content_sha256（`source_fingerprint` 用 frozen document SHA，**不**用
        runtime UUID）。`report_artifact_ref` 恒 None（runtime Report UUID 不进入
        semantic fingerprint）。
        """
        claim_ids, report_payload = await self._load_final_artifacts(
            task_id=task_id, stage4_runner=stage4_runner, stage5_runner=stage5_runner
        )
        claim_uuids = [UUID(c) for c in claim_ids]
        if not claim_uuids:
            raise EvalVariantError("Stage4 未产出任何 claim")

        async with self._sessionmaker() as session:
            claim_rows = (
                (
                    await session.execute(
                        select(ClaimModel).where(ClaimModel.claim_id.in_(claim_uuids))
                    )
                )
                .scalars()
                .all()
            )
            links = (
                (
                    await session.execute(
                        select(ClaimEvidenceLinkModel).where(
                            ClaimEvidenceLinkModel.claim_id.in_(claim_uuids)
                        )
                    )
                )
                .scalars()
                .all()
            )
            card_uuids = sorted({link.evidence_card_id for link in links})
            card_rows = (
                (
                    await session.execute(
                        select(EvidenceCardModel).where(
                            EvidenceCardModel.evidence_card_id.in_(card_uuids)
                        )
                    )
                )
                .scalars()
                .all()
            )

        claim_by_id = {row.claim_id: row for row in claim_rows}
        if len(claim_by_id) != len(claim_uuids):
            raise EvalOutputStructureError("claim_ids 未全部命中本 attempt 的 claims")

        sha_by_source = {doc.source_record_id: doc.content_sha256 for doc in rehydrated.documents}
        # macro-origin 卡：semantic source identity = frozen macro snapshot fingerprint。
        sha_by_macro = {
            macro.snapshot_id: macro.snapshot_fingerprint
            for macro in execution_case.snapshot.macro_snapshots
        }
        # peer replay 卡（comparison 的 peer observation evidence）：source 文档不在
        # frozen snapshot 的 document_sources（peer 文档不冻结），但 content_sha256
        # 冻结在 comparison payload 的 provenance 中——映射到 comparison 的
        # artifact_fingerprint（`valid_source_fingerprints` 已含 structured
        # artifact fingerprints → citation 保持 valid 且可追溯）。
        peer_sha_to_artifact_fp = self._peer_evidence_sha_map(execution_case)
        # attempt DB 内 source → content_sha256（peer replay source 的语义身份）。
        sha_by_attempt_source = await self._attempt_source_sha_map()
        ordered_cards = sorted(card_rows, key=lambda r: str(r.evidence_card_id))
        card_to_key = {
            card.evidence_card_id: f"{CITATION_KEY_PREFIX}{i}"
            for i, card in enumerate(ordered_cards, start=1)
        }

        claims: list[EvalClaim] = []
        citation_claim_ids: dict[str, list[str]] = {}
        for claim_id in claim_ids:
            claim = claim_by_id[UUID(claim_id)]
            claim_links = [link for link in links if str(link.claim_id) == claim_id]
            citation_keys = sorted(
                {card_to_key[link.evidence_card_id] for link in claim_links}, key=_key_rank
            )
            semantic_claim_id = _semantic_claim_id(claim, citation_keys)
            claims.append(
                EvalClaim(
                    claim_id=semantic_claim_id,
                    statement=claim.statement,
                    claim_type=INSIGHTFORGE_FULL_CLAIM_TYPE,
                    citation_ids=tuple(citation_keys),
                )
            )
            for key in citation_keys:
                citation_claim_ids.setdefault(key, []).append(semantic_claim_id)

        citations: list[EvalCitation] = []
        for card in ordered_cards:
            key = card_to_key[card.evidence_card_id]
            source_sha = sha_by_source.get(card.source_id)
            if source_sha is None and card.macro_snapshot_id is not None:
                source_sha = sha_by_macro.get(card.macro_snapshot_id)
            if source_sha is None and card.source_id is not None:
                attempt_sha = sha_by_attempt_source.get(card.source_id)
                if attempt_sha is not None:
                    # peer replay 卡 → 归属其 comparison artifact fingerprint。
                    source_sha = peer_sha_to_artifact_fp.get(attempt_sha, attempt_sha)
            if source_sha is None:
                raise EvalOutputStructureError(
                    f"evidence card 的 source 不在 frozen snapshot：{card.source_id}"
                )
            citations.append(
                EvalCitation(
                    citation_id=key,
                    source_fingerprint=source_sha,
                    locator=_locator_from_card(card),
                    claim_ids=tuple(citation_claim_ids.get(key, ())),
                )
            )

        return EvalVariantOutput(
            variant_id=self.variant_id,
            case_id=execution_case.case_id,
            case_version=execution_case.case_version,
            final_text=_report_to_text(report_payload),
            claims=tuple(claims),
            citations=tuple(citations),
            report_artifact_ref=None,
        )

    def _peer_evidence_sha_map(self, execution_case: LoadedEvalExecutionCase) -> dict[str, str]:
        """frozen comparison payload 的 peer evidence content_sha256 → artifact_fingerprint。

        peer replay 卡 → 其 source 文档 content_sha256 → 归属 comparison artifact。
        """
        mapping: dict[str, str] = {}
        for ref in execution_case.snapshot.structured_artifacts:
            if ref.artifact_type.value != "relative_valuation_comparison":
                continue
            payload = self._loader.load_structured_payload(
                ref.artifact_type, ref.artifact_fingerprint
            )
            provenance = payload.get("provenance") or {}
            for peer in provenance.get("peer_observations", ()):
                evidence = peer.get("evidence") or {}
                inner = evidence.get("evidence") or {}
                sha = inner.get("content_sha256")
                if isinstance(sha, str) and len(sha) == 64:
                    mapping[sha] = ref.artifact_fingerprint
        return mapping

    async def _attempt_source_sha_map(self) -> dict[UUID, str]:
        """attempt DB 内全部 source → raw artifact content_sha256（语义身份）。"""
        from app.db.models.raw_artifact import RawArtifactModel
        from app.db.models.source_record import SourceRecordModel

        async with self._sessionmaker() as session:
            rows = (
                await session.execute(
                    select(SourceRecordModel.source_id, RawArtifactModel.content_sha256).join(
                        RawArtifactModel,
                        RawArtifactModel.artifact_id == SourceRecordModel.artifact_id,
                    )
                )
            ).all()
        return {source_id: content_sha256 for source_id, content_sha256 in rows}

    async def _load_final_artifacts(
        self, *, task_id: UUID, stage4_runner, stage5_runner
    ) -> tuple[list[str], dict]:
        """最终 Stage4 claim_ids + 最终 Report payload（从最新 child checkpoint 读取）。"""
        # 最终 report：latest stage5 child run 的 checkpoint report_id。
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            stage5_runs = await run_repo.list_for_task_by_graph(task_id, _STAGE5_GRAPH_NAME)
        report_payload: dict = {}
        report_id: UUID | None = None
        if stage5_runs:
            latest = stage5_runs[0]
            checkpoint = await stage5_runner.read_checkpoint_state(latest.run_id)
            report_id = _uuid_or_none(checkpoint.get("report_id"))
        if report_id is not None:
            async with self._sessionmaker() as session:
                row = (
                    await session.execute(
                        select(ReportModel).where(ReportModel.report_id == report_id)
                    )
                ).scalar_one_or_none()
            if row is not None:
                report_payload = dict(row.report_payload)
        if not report_payload:
            raise EvalVariantError("insightforge_full 未产出最终 Report")

        # 最终 claim_ids：latest stage4 child run 的 checkpoint。
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            stage4_runs = await run_repo.list_for_task_by_graph(task_id, _STAGE4_GRAPH_NAME)
        claim_ids: list[str] = []
        if stage4_runs:
            latest4 = stage4_runs[0]
            checkpoint4 = await stage4_runner.read_checkpoint_state(latest4.run_id)
            claim_ids = list(checkpoint4.get("claim_ids") or [])
        if not claim_ids:
            raise EvalVariantError("insightforge_full 未产出 Stage4 claims")
        return claim_ids, report_payload


class _RemapAwareFulfillment:
    """包装生产 `ResearchFulfillmentService`：document 证据生成后 inline remap。

    第一轮 fulfill 解析 document / macro needs（生成 attempt EvidenceCard）；
    若尚未 ready（financial / valuation 缺 remapped structured artifacts），先跑
    `StructuredEvidenceRemapService.remap_case`，再第二轮 fulfill（financial /
    valuation executors 消费 remapped 数据）。两轮都走生产服务（幂等 replay，
    不复制业务逻辑）。
    """

    def __init__(
        self,
        inner: ResearchFulfillmentService,
        remap: StructuredEvidenceRemapService,
        execution_case: LoadedEvalExecutionCase,
    ) -> None:
        self._inner = inner
        self._remap = remap
        self._execution_case = execution_case
        self.remap_calls = 0

    async def fulfill_research_needs(self, research_plan_id):
        first = await self._inner.fulfill_research_needs(research_plan_id)
        if first.ready_for_analysis:
            return first
        await self._remap.remap_case(self._execution_case)
        self.remap_calls += 1
        return await self._inner.fulfill_research_needs(research_plan_id)


def _uuid_or_none(value) -> UUID | None:
    if value is None:
        return None
    try:
        return UUID(str(value))
    except (ValueError, TypeError):
        return None


def _semantic_claim_id(claim, citation_keys: list[str]) -> str:
    import hashlib

    payload = {
        "statement": claim.statement,
        "analysis_domain": getattr(claim, "analysis_domain", None),
        "claim_kind": getattr(claim, "claim_kind", None),
        "confidence": getattr(claim, "confidence", None),
        "importance": getattr(claim, "importance", None),
        "citation_keys": sorted(citation_keys),
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()


def _key_rank(key: str) -> int:
    if key.startswith(CITATION_KEY_PREFIX) and key[len(CITATION_KEY_PREFIX) :].isdigit():
        return int(key[len(CITATION_KEY_PREFIX) :])
    return 0


def _locator_from_card(card) -> str | None:
    refs = card.locator_refs or []
    if not refs:
        return None
    first = refs[0] if isinstance(refs[0], dict) else {}
    locator = first.get("locator")
    if isinstance(locator, dict) and locator:
        return json.dumps(locator, sort_keys=True, ensure_ascii=False)
    return f"block {first.get('block_ordinal')}"


def _report_to_text(report_payload: dict) -> str:
    lines: list[str] = []
    for section in report_payload.get("sections", []):
        title = section.get("title", "")
        if title:
            lines.append(title)
        for paragraph in section.get("paragraphs", []):
            text = paragraph.get("text", "") if isinstance(paragraph, dict) else ""
            if text:
                lines.append(text)
    return "\n\n".join(lines).strip()
