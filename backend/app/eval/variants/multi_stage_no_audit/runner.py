"""Multi-stage no-audit variant runner (stage 7B.1.4C.2).

Frozen Bundle → isolated replay → parse/chunk/index → 生产多阶段流水线（到
Stage5 first draft 为止）→ 确定性归一化 `EvalVariantOutput`。

复用（mandatory，不复制逻辑）：
- ResearchPlanningService / ResearchSourceRouter / ResearchFulfillmentService
  （内部 EvidenceExtractionService）/ Stage4 生产 graph（Claim Analysis +
  Synthesis）/ Stage5 first-draft writer 路径（Outline → DraftSections →
  Report）；prompt / fingerprint / Claim 持久化 / Evidence 持久化 / Writer 全部
  走生产代码。

**绝不执行**：run_checks / semantic audit / review routing / revision /
research backflow / human review——Stage5 只做到 first draft + 必要 report
assembly 后 STOP。

隔离不变量：
- rehydration 只落在隔离 target PG + store（`EvaluationReplayRehydrator`）；
- derived index 用 per-attempt 命名空间 collection（绑定 `EvalVariantRuntimeContext`
  `.execution_id`，`eval_multi_stage_no_audit_<hex>`），**不**写回 bundle、**不**
  复用生产 collection；不同 attempt / variant 互相不可见；
- 本 attempt 全部业务数据落在隔离 PG，归一化按本 attempt 的 Stage4 `claim_ids`
  + 本 attempt 的 Report 行读取，不跨 attempt。

模型注入（assembly gate）：`MultiStageModelFactoryBundle` 是唯一的模型构造入口——
runner 在任何 factory call 前校验 bundle.provider / model_id == frozen
`config.model`（不一致 → `EvalExecutionAssemblyError`，0 factory call），随后在
每次 run 内用 bundle 的 5 个 `create_*` factory 创建本 attempt 的生产模型（绑定
config-bound settings + per-attempt usage_observer）。**runner 不 import 任何
DeepSeek adapter**：production factory 创建真实 adapters，E2E / 单测注入 fake
factory bundle。预期 usage 组件 = research_planner / evidence_extraction /
claim_analysis / synthesis_analysis / draft_section_writer；**绝不**出现 audit /
revision_writer。
"""

from __future__ import annotations

import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.db.models.claim import ClaimModel
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.report import ReportModel
from app.db.models.research_task import ResearchTaskModel
from app.draft_section.contracts import DraftSectionRequest
from app.draft_section.service import DraftSectionService
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
    EvalMultiStageNoAuditInputError,
    EvalMultiStageNoAuditPlanError,
    EvalOutputStructureError,
    EvalVariantError,
)
from app.eval.execution.contracts import EvalVariantRuntimeContext
from app.eval.fingerprints import compute_execution_config_fingerprint
from app.eval.replay.rehydrator import EvaluationReplayRehydrator
from app.eval.variants import EvalVariantId
from app.eval.variants.multi_stage_no_audit.contracts import (
    CITATION_KEY_PREFIX,
    MULTI_STAGE_NO_AUDIT_CLAIM_TYPE,
    MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
    MultiStageModelFactoryBundle,
)
from app.llm.instrumentation import LlmUsageObserver
from app.rag.embedding.contracts import BGE_SMALL_ZH_V1_5, EmbeddingProvider
from app.rag.index.service import VectorIndexService
from app.rag.retrieval.service import RetrievalService
from app.report.contracts import ReportAssemblyDraft
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    SourceIndexBuilder,
    ValuationNeedExecutor,
)
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_planning.contracts import ResearchPlanPayload
from app.research_planning.preparation import ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.chunking_service import ChunkingService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_parsing_service import SourceParsingService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.graph import build_stage4_analysis_graph
from app.vectorstore.client import ChromaManager


class MultiStageNoAuditVariantRunner:
    variant_id = EvalVariantId.MULTI_STAGE_NO_AUDIT

    def __init__(
        self,
        *,
        config: EvalExecutionConfig,
        rehydrator: EvaluationReplayRehydrator,
        parsing_service: SourceParsingService,
        chunking_service: ChunkingService,
        sessionmaker: async_sessionmaker,
        embedding_provider: EmbeddingProvider,
        chroma: ChromaManager,
        model_factory_bundle: MultiStageModelFactoryBundle,
    ) -> None:
        # 构造期绑定 config；variant / prompt version 不匹配 = 装配错误（0 model call）。
        if config.variant_id != self.variant_id:
            raise EvalExecutionAssemblyError("config.variant_id 不是 multi_stage_no_audit")
        if config.prompt_version != MULTI_STAGE_NO_AUDIT_PROMPT_VERSION:
            raise EvalExecutionAssemblyError(
                f"config.prompt_version {config.prompt_version!r} != "
                f"{MULTI_STAGE_NO_AUDIT_PROMPT_VERSION!r}"
            )
        self._config = config
        self._rehydrator = rehydrator
        self._parsing = parsing_service
        self._chunking = chunking_service
        self._sessionmaker = sessionmaker
        self._embedding = embedding_provider
        self._chroma = chroma
        self._bundle = model_factory_bundle

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
        # 2. 模型身份 preflight：注入 bundle + embedding provider 必须匹配
        #    frozen config / 冻结 BGE 模型（0 model call）。
        self._validate_model_identity()
        self._validate_embedding_identity()
        # 3. input closure（document-only，0 model call）。
        self._validate_input(execution_case)

        # 4. rehydrate：frozen bundle → 隔离 PG + store。
        rehydrated = await self._rehydrator.rehydrate_case(
            execution_case.case_id, execution_case.case_version
        )
        if not rehydrated.documents:
            raise EvalMultiStageNoAuditInputError("rehydration 未产出任何 document source")

        # 5. per-attempt collection 命名空间 + manifest runtime_scope（均绑定
        #    execution_id；派生索引，不写回 bundle，不同 attempt / variant 互不可见）。
        collection_name = self._collection_name(runtime_context.execution_id)
        runtime_scope = f"eval:multi_stage_no_audit:{runtime_context.execution_id.hex}"
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

        # 6. parse → chunk → index（每条 frozen document 走真实 deterministic
        #    pipeline；parse/chunk 幂等 create-or-get，manifest 绑定 runtime_scope）。
        for doc in rehydrated.documents:
            parsed = await self._parsing.parse_source(doc.source_record_id)
            chunked = await self._chunking.chunk_parsed_source(parsed.parsed_source_id)
            await index_service.index_chunk_set(chunked.chunk_set_id, force_rebuild=True)

        # 7. per-attempt 模型构造：只用 bundle 的 factory（每个传入本 attempt 的
        #    usage_observer），不 import 任何生产 adapter。create_claim /
        #    create_synthesis 由 create_stage4_deps 内部复用（Stage4 deps 组装）。
        planner_model = self._bundle.create_planner(usage_observer)
        evidence_model = self._bundle.create_evidence(usage_observer)
        draft_model = self._bundle.create_draft(usage_observer)
        deps4 = self._bundle.create_stage4_deps(self._sessionmaker, usage_observer)

        # per-attempt 生产服务（纯组装，模型已由 bundle factory 提供）。
        plan_service = ResearchPlanningService(
            self._sessionmaker, planner_model, CompanyIdentityService(self._sessionmaker)
        )
        router = ResearchSourceRouter(self._sessionmaker, plan_service)
        preparation = ResearchPreparationService(self._sessionmaker, plan_service, router)
        index_builder = SourceIndexBuilder(self._sessionmaker, self._chunking, index_service)
        document_executor = DocumentNeedExecutor(
            self._sessionmaker, retrieval_service, evidence_model, index_builder=index_builder
        )
        fulfillment = ResearchFulfillmentService(
            self._sessionmaker,
            plan_service,
            router,
            preparation,
            document_executor=document_executor,
            financial_executor=FinancialNeedExecutor(self._sessionmaker),
            macro_executor=MacroNeedExecutor(self._sessionmaker),
            valuation_executor=ValuationNeedExecutor(),
        )

        # 8. 隔离 PG 内创建 ResearchTask（planner/fulfillment 都 keyed off task）。
        task_id = await self._create_research_task(execution_case)

        # 9. plan → route → fulfill（只消费 document need；非 document need 已
        #    fail-fast；fulfill 内部走 EvidenceExtractionService）。
        plan_result = await plan_service.create_plan(task_id)
        self._validate_plan(plan_result)
        await router.route_research_plan(plan_result.research_plan_id)
        fulfillment_result = await fulfillment.fulfill_research_needs(plan_result.research_plan_id)
        if not fulfillment_result.ready_for_analysis or fulfillment_result.stage4_request is None:
            raise EvalVariantError("fulfillment 未达到 analysis-ready（无 stage4_request）")

        # 10. Stage4：生产 graph（Claim Analysis → Synthesis）。deps 来自 step 7
        #     （bundle factory 组装，claim/synthesis 模型已线程 observer）。
        stage4_request = Stage4WorkflowRequest.model_validate(fulfillment_result.stage4_request)
        graph4 = build_stage4_analysis_graph(deps4)
        final_state = await graph4.ainvoke(self._stage4_initial_state(stage4_request))
        synthesis_result_id = final_state.get("synthesis_result_id")
        if not synthesis_result_id:
            raise EvalVariantError("Stage4 未产出 synthesis_result_id")

        # 11. Stage5 first draft：Outline → 逐 section Draft → Report assembly。
        #     只走 build_report_draft + assemble_report 的路径，之后 STOP——
        #     不调 check / audit / review / revision / backflow。
        report_id, report_payload = await self._build_first_draft(
            synthesis_result_id, draft_model=draft_model
        )

        # 12. 确定性归一化（无额外 LLM）。
        return await self._normalize(execution_case, rehydrated, final_state, report_payload)

    # ------------------------------------------------------------------ 内部

    def _validate_spec(self, execution_spec: EvalExecutionSpec) -> None:
        if execution_spec.variant_id != self.variant_id:
            raise EvalExecutionAssemblyError("execution_spec.variant_id 不是 multi_stage_no_audit")
        if (
            compute_execution_config_fingerprint(self._config)
            != execution_spec.execution_config_fingerprint
        ):
            raise EvalExecutionAssemblyError(
                "execution_config_fingerprint 与 runner 绑定 config 不一致"
            )

    def _validate_model_identity(self) -> None:
        """模型身份 preflight：注入 bundle 必须等于 frozen config（0 model call）。

        确保同一个 execution_config 下不会注入错误 provider / model_id 的模型集；
        不一致 = benchmark assembly corruption，抛 `EvalExecutionAssemblyError`。
        5 个模型实例在生产工厂构造时绑定 config-bound settings，因此 bundle 级
        provider / model_id 校验足以证明整组身份一致。
        """
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
        """Embedding 模型身份 preflight（0 model call）。与 single_rag 一致。

        multi_stage_no_audit 的 index/retrieval 同样冻结为 BGE：注入 provider 的
        `model_info`（model_id + immutable revision）必须等于 `BGE_SMALL_ZH_V1_5`。
        """
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
            raise EvalMultiStageNoAuditInputError(
                "multi_stage_no_audit v1 需要 >=1 条 document source"
            )
        if snapshot.macro_snapshots:
            raise EvalMultiStageNoAuditInputError("multi_stage_no_audit v1 不支持 macro 输入")
        if snapshot.structured_artifacts:
            raise EvalMultiStageNoAuditInputError("multi_stage_no_audit v1 不支持 structured 输入")

    @staticmethod
    def _validate_plan(plan_result) -> None:
        """plan 只支持 document-only：financial / macro / valuation need 任一
        非空（需要 live acquisition，frozen 快照无法满足）→ 稳定 fail-fast
        `multi_stage_no_audit_plan_not_supported`。event need 由生产
        DocumentNeedExecutor 从文档满足，不算非 document need。"""
        payload = ResearchPlanPayload.model_validate(plan_result.plan_payload)
        if payload.financial_needs or payload.macro_needs or payload.valuation_needs:
            raise EvalMultiStageNoAuditPlanError(
                "multi_stage_no_audit v1 只支持 document-only 计划"
            )
        if not payload.document_needs:
            raise EvalMultiStageNoAuditPlanError(
                "multi_stage_no_audit v1 计划必须至少 1 条 document need"
            )

    @staticmethod
    def _collection_name(execution_id: UUID) -> str:
        # 派生索引命名空间绑定 execution_id（与 single_rag 同约定）：
        # 不同 attempt / retry / trial 各有独立 collection，互不可见。
        return f"eval_multi_stage_no_audit_{execution_id.hex}"

    @staticmethod
    def _stage4_initial_state(stage4_request: Stage4WorkflowRequest) -> dict:
        """镜像生产 `Stage4WorkflowRunner._build_initial_state`（checkpoint-safe）。"""
        return {
            "company_id": str(stage4_request.company_id),
            "research_question": stage4_request.research_question,
            "analysis_as_of": stage4_request.analysis_as_of.isoformat(),
            "analysis_work_items": [
                item.model_dump(mode="json") for item in stage4_request.analysis_work_items
            ],
            "analysis_results": [],
            "claim_ids": [],
            "synthesis_id": None,
            "synthesis_result_id": None,
        }

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

    async def _build_first_draft(
        self,
        synthesis_result_id: str,
        *,
        draft_model,
    ) -> tuple[UUID, dict]:
        """Stage5 first draft：Outline → 逐 section Draft → Report assembly。

        镜像生产 `make_build_report_draft_node` + `make_assemble_report_node`，
        到 Report 为止 STOP（不调 check / audit / review / revision / backflow）。
        `draft_model` 来自 bundle factory（已线程本 attempt 的 usage_observer）。
        返回 (report_id, report_payload)。
        """
        outline_service = ReportOutlineService(self._sessionmaker)
        draft_section_service = DraftSectionService(self._sessionmaker, draft_model)
        report_service = ReportService(self._sessionmaker, draft_section_service)

        outline = await outline_service.create_or_get_outline(UUID(synthesis_result_id))
        verified = await outline_service.verify_outline_integrity(_as_uuid(outline.outline_id))
        sections: list[dict] = []
        for section in verified.sections:
            result = await draft_section_service.create_or_get_section(
                DraftSectionRequest(
                    outline_id=_as_uuid(verified.outline_id), section_id=section.section_id
                )
            )
            sections.append(
                {
                    "section_order": section.section_order,
                    "draft_section_id": str(result.draft_section_id),
                }
            )
        ordered = sorted(sections, key=lambda s: s.get("section_order", 0))
        draft_section_ids = tuple(UUID(s["draft_section_id"]) for s in ordered)
        assembled = await report_service.create_or_get_report(
            ReportAssemblyDraft(
                outline_id=_as_uuid(outline.outline_id), draft_section_ids=draft_section_ids
            )
        )
        report_payload = await self._load_report_payload(assembled.report_id)
        return assembled.report_id, report_payload

    async def _load_report_payload(self, report_id: UUID) -> dict:
        async with self._sessionmaker() as session:
            row = (
                await session.execute(select(ReportModel).where(ReportModel.report_id == report_id))
            ).scalar_one()
            return dict(row.report_payload)

    async def _normalize(
        self,
        execution_case: LoadedEvalExecutionCase,
        rehydrated,
        final_state: dict,
        report_payload: dict,
    ) -> EvalVariantOutput:
        """确定性归一化（无额外 LLM）：本 attempt 的 Claims / EvidenceCards /
        Draft/Report → `EvalVariantOutput`。citation 链 =
        EvidenceCard → SourceRecord → FrozenDocumentSourceRef → content_sha256
        （`source_fingerprint` 用 frozen document SHA，**不**用 SourceRecord UUID）。

        `report_artifact_ref` 恒为 None：`compute_variant_output_fingerprint` 把
        `report_artifact_ref` 纳入 fingerprint，而 Report UUID 是 runtime 身份
        （同语义输出每次 attempt 不同），放进 fingerprint 会破坏跨 attempt 的
        semantic 可比性。v1 不提供稳定的 semantic report fingerprint，因此不输出。
        """
        claim_ids = sorted(set(final_state.get("claim_ids") or []))
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

        # citation 稳定排序：按 str(evidence_card_id) → E1 / E2 / ...（不进 prompt，
        # application 侧映射 content_sha256）。
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
            # normalized claim identity = **语义** key（statement / domain / kinds /
            # citation keys 的确定性哈希）。runtime claim UUID 与 claim_fingerprint
            # 都绑定本 attempt 的 evidence card UUID（跨 attempt 不同），放进
            # fingerprint 会破坏跨 attempt 语义可比性——与 fingerprint 规则
            # 「排除 runtime identity」一致。
            semantic_claim_id = _semantic_claim_id(claim, citation_keys)
            claims.append(
                EvalClaim(
                    claim_id=semantic_claim_id,
                    statement=claim.statement,
                    claim_type=MULTI_STAGE_NO_AUDIT_CLAIM_TYPE,
                    citation_ids=tuple(citation_keys),
                )
            )
            for key in citation_keys:
                citation_claim_ids.setdefault(key, []).append(semantic_claim_id)

        citations: list[EvalCitation] = []
        for card in ordered_cards:
            key = card_to_key[card.evidence_card_id]
            source_sha = sha_by_source.get(card.source_id)
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


def _semantic_claim_id(claim, citation_keys: list[str]) -> str:
    """确定性语义 claim identity：statement + domain/kinds + citation keys 哈希。

    runtime claim UUID / DB claim_fingerprint 都绑定本 attempt 的 evidence card
    UUID，跨 attempt 不稳定；本函数只消费语义字段（citation keys 已排序），
    同语义 → 同 id → 同 normalized output fingerprint。
    """
    payload = {
        "statement": claim.statement,
        "analysis_domain": getattr(claim, "analysis_domain", None),
        "claim_kind": getattr(claim, "claim_kind", None),
        "confidence": getattr(claim, "confidence", None),
        "importance": getattr(claim, "importance", None),
        "citation_keys": sorted(citation_keys),
    }
    return hashlib.sha256(canonical_json_str(payload).encode("utf-8")).hexdigest()


def _as_uuid(value: UUID | str) -> UUID:
    """归一化 UUID：生产 service 返回 SQLAlchemy UUID 对象，单测 fake 可能返回 str。"""
    return value if isinstance(value, UUID) else UUID(value)


def _key_rank(key: str) -> int:
    """`E{rank}` → 数值 rank（用于 citation 稳定排序；非 E-key 回退 0）。"""
    if key.startswith(CITATION_KEY_PREFIX) and key[len(CITATION_KEY_PREFIX) :].isdigit():
        return int(key[len(CITATION_KEY_PREFIX) :])
    return 0


def _locator_from_card(card) -> str | None:
    """从真实 EvidenceCard 的 locator_refs 派生稳定 locator（block/page 定位）。"""
    refs = card.locator_refs or []
    if not refs:
        return None
    first = refs[0] if isinstance(refs[0], dict) else {}
    locator = first.get("locator")
    if isinstance(locator, dict) and locator:
        return json.dumps(locator, sort_keys=True, ensure_ascii=False)
    return f"block {first.get('block_ordinal')}"


def _report_to_text(report_payload: dict) -> str:
    """报告正文：section title + 逐段 text（确定性拼接，0 LLM）。"""
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
