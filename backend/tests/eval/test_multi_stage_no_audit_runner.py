"""Multi-stage no-audit variant runner 单元测试（stage 7B.1.4C.2）。

全部离线：fake rehydrator / parsing / chunking / model factory bundle / 重 service
+ monkeypatch `VectorIndexService` / `RetrievalService` / 生产编排服务，0 DB /
0 LLM / 0 network / 0 Chroma。

覆盖 multi_stage_no_audit runner 的：
- **Model injection gate**：bundle 身份校验在**任何 factory call 前**（wrong
  provider / model_id → `EvalExecutionAssemblyError`，0 factory call）；correct
  identity → 5 个 factory 收到**同一个** per-attempt usage_observer；fake factories
  被实际调用（runner 不偷偷创建真实 DeepSeek adapter——AST 断言无生产 adapter import）；
- input closure（0 document / macro / structured → `EvalMultiStageNoAuditInputError`，
  0 factory call）；
- plan closure（financial / macro / valuation plan → `EvalMultiStageNoAuditPlanError`；
  document plan / event need → 允许继续 document executor 路径）；
- 每 attempt 隔离（collection + manifest runtime_scope 绑定 execution_id）；
- stage boundary（调用顺序 planner → router → fulfillment → stage4 → first draft；
  无 audit / review / revision / backflow import 或调用）；
- normalization gate（citation `source_fingerprint` == frozen `content_sha256`，
  双向 closure 通过 `analyze_citations` / `verify_variant_output_identity`）；
- report_artifact_ref semantic rule（v1 恒 None；`compute_variant_output_fingerprint`
  不因 runtime Report UUID 变化）。
"""

import ast
import importlib.util
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from app.eval.bundle.loader import LoadedEvalExecutionCase
from app.eval.contracts import (
    EvalExecutionConfig,
    EvalExecutionSpec,
    FrozenCompanyIdentity,
    FrozenDocumentSourceRef,
    FrozenModelConfig,
    FrozenSourceSnapshot,
    FrozenStructuredArtifactRef,
    StructuredArtifactType,
)
from app.eval.errors import (
    EvalExecutionAssemblyError,
    EvalMultiStageNoAuditInputError,
    EvalMultiStageNoAuditPlanError,
)
from app.eval.execution.contracts import EvalVariantRuntimeContext
from app.eval.fingerprints import (
    compute_execution_config_fingerprint,
    compute_variant_output_fingerprint,
)
from app.eval.replay.contracts import RehydratedCase, RehydratedDocument
from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import (
    analyze_citations,
    verify_variant_output_identity,
)
from app.eval.usage.collector import EvalLlmUsageCollector
from app.eval.variants import EvalVariantId
from app.eval.variants.multi_stage_no_audit import (
    CITATION_KEY_PREFIX,
    MULTI_STAGE_NO_AUDIT_CLAIM_TYPE,
    MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
    MultiStageModelFactoryBundle,
)
from app.eval.variants.multi_stage_no_audit.runner import MultiStageNoAuditVariantRunner
from app.financial.calculations.contracts import CalculationCode
from app.financial.contracts import MetricCode
from app.research_planning.contracts import (
    AnalysisModule,
    PeerPolicy,
    ResearchDocumentNeedType,
    ResearchScope,
    ValuationNeedMetric,
)
from app.research_planning.service import ResearchPlanResult
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.eval.macro_factory import make_macro_ref

CASE_ID = "multi-stage-case"
CASE_FP = "c" * 64
SNAP_FP = "d" * 64
UID = UUID("00000000-0000-0000-0000-000000000001")
SOURCE_RECORD_ID = UUID("00000000-0000-0000-0000-000000000002")
RAW_ARTIFACT_ID = UUID("00000000-0000-0000-0000-000000000003")
PARSED_SOURCE_ID = UUID("00000000-0000-0000-0000-000000000004")
CHUNK_SET_ID = UUID("00000000-0000-0000-0000-000000000005")
TASK_ID = UUID("00000000-0000-0000-0000-000000000006")
PLAN_ID = UUID("00000000-0000-0000-0000-000000000007")
SYNTHESIS_ID = UUID("00000000-0000-0000-0000-000000000008")
OUTLINE_ID = UUID("00000000-0000-0000-0000-000000000009")
REPORT_ID = UUID("00000000-0000-0000-0000-00000000000a")
DRAFT_SECTION_ID = UUID("00000000-0000-0000-0000-00000000000b")
DRAFT_SECTION_ID_2 = UUID("00000000-0000-0000-0000-00000000000c")
CLAIM1 = UUID("00000000-0000-0000-0000-000000000011")
CLAIM2 = UUID("00000000-0000-0000-0000-000000000012")
CARD1 = UUID("00000000-0000-0000-0000-000000000013")
CARD2 = UUID("00000000-0000-0000-0000-000000000014")
DOC_SHA = "a" * 64

# 与 frozen snapshot 内容无关的合法 fingerprint 占位。
REPORT_PAYLOAD = {
    "sections": [
        {
            "title": "营收分析",
            "paragraphs": [{"text": "营收同比增长 18%"}, {"text": "毛利率 55%"}],
        }
    ]
}
FINAL_TEXT = "营收分析\n\n营收同比增长 18%\n\n毛利率 55%"


# ---------------------------------------------------------------- 构建 helpers


def _make_config(**overrides) -> EvalExecutionConfig:
    kwargs = dict(
        variant_id=EvalVariantId.MULTI_STAGE_NO_AUDIT,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-chat",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="v1",
        prompt_version=MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
        retrieval_version="v1",
        pipeline_version="v1",
        retrieval_top_k=4,
    )
    kwargs.update(overrides)
    return EvalExecutionConfig(**kwargs)


def _doc_ref(content_sha256: str = DOC_SHA) -> FrozenDocumentSourceRef:
    return FrozenDocumentSourceRef(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=content_sha256,
        provider_key="cninfo",
        document_type="annual_report",
        media_type="application/pdf",
        title="测试文档",
        source_url="https://example.com/doc",
        acquired_at=datetime(2026, 8, 1, 12, 0, 0),
        authority_tier_snapshot=3,
        critical_claim_eligible_snapshot=True,
    )


def _company() -> FrozenCompanyIdentity:
    return FrozenCompanyIdentity(
        security_code="600519",
        official_name="测试公司",
        exchange="SSE",
        board="sse_main",
    )


def _snapshot(**overrides) -> FrozenSourceSnapshot:
    kwargs = dict(
        document_sources=(_doc_ref(),),
        macro_snapshots=(),
        structured_artifacts=(),
    )
    kwargs.update(overrides)
    return FrozenSourceSnapshot(**kwargs)


def _case(snapshot: FrozenSourceSnapshot | None = None) -> LoadedEvalExecutionCase:
    return LoadedEvalExecutionCase(
        case_fingerprint=CASE_FP,
        case_id=CASE_ID,
        case_version=1,
        company_id=UID,
        company=_company(),
        research_question="贵州茅台 2024 年营收增长如何？",
        analysis_as_of=datetime(2026, 8, 1, 12, 0, 0),
        tags=(),
        snapshot=snapshot if snapshot is not None else _snapshot(),
    )


def _spec(
    config: EvalExecutionConfig, *, config_fingerprint: str | None = None
) -> EvalExecutionSpec:
    return EvalExecutionSpec(
        case_fingerprint=CASE_FP,
        source_snapshot_fingerprint=SNAP_FP,
        execution_config_fingerprint=(
            config_fingerprint
            if config_fingerprint is not None
            else compute_execution_config_fingerprint(config)
        ),
        variant_id=EvalVariantId.MULTI_STAGE_NO_AUDIT,
    )


def _rehydrated_doc() -> RehydratedDocument:
    return RehydratedDocument(
        source_record_id=SOURCE_RECORD_ID,
        raw_artifact_id=RAW_ARTIFACT_ID,
        content_sha256=DOC_SHA,
        storage_key="blobs/sha256/aa/" + DOC_SHA,
        byte_size=128,
        media_type="application/pdf",
    )


def _runtime(*, execution_id: UUID | None = None, attempt_no: int = 1) -> EvalVariantRuntimeContext:
    return EvalVariantRuntimeContext(
        execution_id=execution_id if execution_id is not None else UID,
        trial_fingerprint="d" * 64,
        attempt_no=attempt_no,
    )


def _plan_payload(
    *,
    document: bool = True,
    financial: bool = False,
    macro: bool = False,
    valuation: bool = False,
    event: bool = False,
) -> dict:
    """构造一个可被 `ResearchPlanPayload.model_validate` 接受的 plan payload dict。"""
    return {
        "research_scope": [ResearchScope.BUSINESS.value],
        "analysis_modules": [AnalysisModule.BUSINESS_EVENT.value],
        "document_needs": (
            []
            if not document
            else [
                {
                    "need_code": "doc_annual",
                    "purpose": "年度报告",
                    "source_type": ResearchDocumentNeedType.ANNUAL_REPORT.value,
                    "period": "2024",
                }
            ]
        ),
        "financial_needs": (
            []
            if not financial
            else [
                {
                    "need_code": "fin_rev",
                    "purpose": "营收增长",
                    "calculation_code": CalculationCode.YOY_GROWTH_RATE.value,
                    "metric_code": MetricCode.REVENUE.value,
                    "period": "2024",
                }
            ]
        ),
        "macro_needs": (
            []
            if not macro
            else [
                {
                    "need_code": "macro_gdp",
                    "purpose": "宏观驱动",
                    "topic_or_indicator": "GDP 增速",
                    "geography": None,
                }
            ]
        ),
        "event_needs": (
            [] if not event else [{"need_code": "evt_ma", "purpose": "并购事件", "topic": "并购"}]
        ),
        "valuation_needs": (
            []
            if not valuation
            else [
                {
                    "need_code": "val_pe",
                    "metric_code": ValuationNeedMetric.PE_TTM.value,
                    "peer_policy": PeerPolicy.PEER_MEDIAN.value,
                }
            ]
        ),
        "research_focus": [],
    }


# ---------------------------------------------------------------- fakes


class _FakeModel:
    """fake 模型实例：只有身份，没有任何 LLM 行为。"""

    def __init__(self, name: str) -> None:
        self.name = name
        self.calls = 0


def _counting_bundle(
    *, provider: str = "deepseek", model_id: str = "deepseek-chat"
) -> tuple[MultiStageModelFactoryBundle, list]:
    """真实 `MultiStageModelFactoryBundle` + 记录每次 factory 调用 (name, observer)。

    `create_stage4_deps` 镜像生产结构：内部复用 create_claim / create_synthesis。
    """
    calls: list[tuple[str, object]] = []
    models = {
        name: _FakeModel(name) for name in ("planner", "evidence", "claim", "synthesis", "draft")
    }
    bundle: MultiStageModelFactoryBundle | None = None

    def _rec(name: str):
        def _call(observer=None):
            calls.append((name, observer))
            return models[name]

        return _call

    def _stage4_deps(sessionmaker, observer=None):
        # 复用 bundle 自身的 claim / synthesis factories（fakes 传播进 Stage4）。
        bundle.create_claim(observer)
        bundle.create_synthesis(observer)
        return SimpleNamespace(
            sessionmaker=sessionmaker,
            claim_model=models["claim"],
            synthesis_model=models["synthesis"],
        )

    bundle = MultiStageModelFactoryBundle(
        provider=provider,
        model_id=model_id,
        create_planner=_rec("planner"),
        create_evidence=_rec("evidence"),
        create_claim=_rec("claim"),
        create_synthesis=_rec("synthesis"),
        create_draft=_rec("draft"),
        create_stage4_deps=_stage4_deps,
    )
    return bundle, calls


class _FakeResult:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return self._rows

    def scalar_one(self):
        if not self._rows:
            raise IndexError("scalar_one on empty result")
        return self._rows[0]

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按 select 目标表名分发 canned rows 的假 AsyncSession。"""

    def __init__(self, rows: dict[str, list]) -> None:
        self._rows = rows
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def execute(self, statement):
        table = statement.froms[0].name
        return _FakeResult(self._rows.get(table, []))

    async def commit(self):
        self.commits += 1

    async def flush(self):
        return None

    def add(self, obj):
        return None


class _FakeSessionmaker:
    def __init__(self, rows: dict[str, list] | None = None) -> None:
        self._rows = rows or {}
        self.sessions: list[_FakeSession] = []

    def __call__(self) -> _FakeSession:
        session = _FakeSession(self._rows)
        self.sessions.append(session)
        return session


class _FakeRehydrator:
    def __init__(self, documents=()) -> None:
        self._documents = tuple(documents)
        self.calls = 0

    async def rehydrate_case(self, case_id, case_version):
        self.calls += 1
        return RehydratedCase(company_id=UID, provider_keys=("cninfo",), documents=self._documents)


class _FakeParsing:
    def __init__(self) -> None:
        self.calls = 0

    async def parse_source(self, source_record_id):
        self.calls += 1
        return SimpleNamespace(parsed_source_id=PARSED_SOURCE_ID)


class _FakeChunking:
    def __init__(self) -> None:
        self.calls = 0

    async def chunk_parsed_source(self, parsed_source_id):
        self.calls += 1
        return SimpleNamespace(chunk_set_id=CHUNK_SET_ID)


class _FakeVectorIndexService:
    def __init__(
        self, sessionmaker, embedding, chroma, collection_name=None, *, runtime_scope="production"
    ) -> None:
        self.collection_name = collection_name
        self.runtime_scope = runtime_scope
        self.index_calls = []

    async def index_chunk_set(self, chunk_set_id, *, force_rebuild=False) -> None:
        self.index_calls.append((chunk_set_id, force_rebuild))
        return None


class _FakeRetrievalService:
    def __init__(self, sessionmaker, embedding, chroma, collection_name=None) -> None:
        self.collection_name = collection_name
        self.retrieve_calls = []

    async def retrieve(self, query):
        self.retrieve_calls.append(query)
        return []


def _patch_rag(monkeypatch) -> dict:
    """monkeypatch runner 模块里的 VectorIndexService / RetrievalService，返回实例 holder。"""
    created: dict = {}

    def vec_factory(
        sessionmaker, embedding, chroma, collection_name=None, *, runtime_scope="production"
    ):
        inst = _FakeVectorIndexService(
            sessionmaker, embedding, chroma, collection_name, runtime_scope=runtime_scope
        )
        created["vector"] = inst
        return inst

    def ret_factory(sessionmaker, embedding, chroma, collection_name=None):
        inst = _FakeRetrievalService(sessionmaker, embedding, chroma, collection_name)
        created["retrieval"] = inst
        return inst

    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.VectorIndexService", vec_factory
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.RetrievalService", ret_factory
    )
    return created


def _patch_pipeline(monkeypatch, *, trace: list, plan_payload: dict, final_state: dict) -> dict:
    """monkeypatch runner 模块里的全部重编排服务，记录调用顺序，返回捕获实例。

    - planner → router → fulfillment → stage4 → first draft 的调用顺序进 `trace`；
    - `build_stage4_analysis_graph` 被替换为 fake graph（`ainvoke` 返回 final_state）；
    - DB 落点（ResearchTask / Outline / DraftSection / Report）全部 fake。
    """
    captured: dict = {}

    class _FakeTaskRepository:
        def __init__(self, session):
            self._session = session

        async def create(self, task):
            trace.append("task.create")
            return SimpleNamespace(task_id=TASK_ID)

    class _FakePlanningService:
        def __init__(self, sessionmaker, planner_model, company_identity):
            captured["planner_model"] = planner_model

        async def create_plan(self, task_id):
            trace.append("plan.create_plan")
            return ResearchPlanResult(
                research_plan_id=PLAN_ID,
                replayed=False,
                plan_schema_version=1,
                planner_name="fake",
                planner_version=1,
                model_id="deepseek-chat",
                planner_input_fingerprint="f" * 64,
                plan_fingerprint="g" * 64,
                plan_payload=plan_payload,
                created_at=datetime(2026, 8, 1, 12, 0, 0),
            )

    class _FakeRouter:
        def __init__(self, sessionmaker, plan_service):
            pass

        async def route_research_plan(self, research_plan_id):
            trace.append("plan.route_research_plan")

    class _FakePreparation:
        def __init__(self, sessionmaker, plan_service, router):
            pass

    class _FakeSourceIndexBuilder:
        def __init__(self, sessionmaker, chunking_service, index_service):
            pass

    class _FakeDocumentExecutor:
        def __init__(self, sessionmaker, retrieval_service, extractor_model, index_builder=None):
            captured["evidence_model"] = extractor_model

    class _FakeNoopExecutor:
        def __init__(self, *args, **kwargs):
            pass

    class _FakeFulfillment:
        def __init__(
            self,
            sessionmaker,
            plan_service,
            router,
            preparation,
            document_executor,
            financial_executor,
            macro_executor,
            valuation_executor,
        ):
            pass

        async def fulfill_research_needs(self, research_plan_id):
            trace.append("fulfill.fulfill_research_needs")
            return SimpleNamespace(
                ready_for_analysis=True,
                stage4_request={
                    "task_id": str(TASK_ID),
                    "company_id": str(UID),
                    "research_question": "贵州茅台 2024 年营收增长如何？",
                    "analysis_as_of": "2026-08-01",
                    "analysis_work_items": [
                        {
                            "item_id": "w1",
                            "analysis_type": "business",
                            "evidence_card_ids": [str(CARD1), str(CARD2)],
                        }
                    ],
                },
            )

    class _FakeGraph:
        async def ainvoke(self, initial_state):
            trace.append("stage4.ainvoke")
            captured["stage4_initial_state"] = initial_state
            return dict(final_state)

    def _build_graph(deps4, checkpointer=None):
        trace.append("stage4.build_graph")
        captured["deps4"] = deps4
        return _FakeGraph()

    class _FakeOutlineService:
        def __init__(self, sessionmaker):
            pass

        async def create_or_get_outline(self, synthesis_result_id):
            trace.append("draft.create_or_get_outline")
            return SimpleNamespace(outline_id=str(OUTLINE_ID))

        async def verify_outline_integrity(self, outline_id):
            trace.append("draft.verify_outline_integrity")
            # runner 把 verified.outline_id 直接传入 DraftSectionRequest（要求真实 UUID）。
            return SimpleNamespace(
                outline_id=OUTLINE_ID,
                sections=[
                    SimpleNamespace(section_id="sec-1", section_order=1),
                    SimpleNamespace(section_id="sec-2", section_order=2),
                ],
            )

    class _FakeDraftSectionService:
        def __init__(self, sessionmaker, draft_model):
            captured["draft_model"] = draft_model
            self._counter = 0

        async def create_or_get_section(self, request):
            trace.append("draft.create_or_get_section")
            self._counter += 1
            # 两个 section 各自独立 draft_section_id（ReportAssemblyDraft 要求唯一）。
            return SimpleNamespace(
                draft_section_id=(DRAFT_SECTION_ID if self._counter == 1 else DRAFT_SECTION_ID_2)
            )

    class _FakeReportService:
        def __init__(self, sessionmaker, draft_section_service):
            pass

        async def create_or_get_report(self, draft):
            trace.append("draft.create_or_get_report")
            return SimpleNamespace(report_id=REPORT_ID)

    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ResearchTaskRepository",
        _FakeTaskRepository,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ResearchPlanningService",
        _FakePlanningService,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ResearchSourceRouter", _FakeRouter
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ResearchPreparationService",
        _FakePreparation,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.SourceIndexBuilder",
        _FakeSourceIndexBuilder,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.DocumentNeedExecutor",
        _FakeDocumentExecutor,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.FinancialNeedExecutor",
        _FakeNoopExecutor,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.MacroNeedExecutor", _FakeNoopExecutor
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ValuationNeedExecutor",
        _FakeNoopExecutor,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ResearchFulfillmentService",
        _FakeFulfillment,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.build_stage4_analysis_graph",
        _build_graph,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ReportOutlineService",
        _FakeOutlineService,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.DraftSectionService",
        _FakeDraftSectionService,
    )
    monkeypatch.setattr(
        "app.eval.variants.multi_stage_no_audit.runner.ReportService", _FakeReportService
    )
    return captured


def _normalization_rows() -> dict[str, list]:
    """normalize / load-report 阶段按表分发的 canned rows（与 final_state 一致）。

    claim 行带 `analysis_domain` / `claim_kind` / `confidence` / `importance`
    （`_semantic_claim_id` 的语义输入），**不**带 claim_fingerprint——normalized
    claim_id 由 runner 从语义字段确定性派生（跨 attempt 稳定），不依赖 DB 行
    fingerprint。
    """
    return {
        "claims": [
            SimpleNamespace(
                claim_id=CLAIM1,
                statement="营收同比增长 18%",
                analysis_domain="business",
                claim_kind="fact",
                confidence="high",
                importance="normal",
            ),
            SimpleNamespace(
                claim_id=CLAIM2,
                statement="毛利率 55%",
                analysis_domain="business",
                claim_kind="inference",
                confidence="medium",
                importance="normal",
            ),
        ],
        "claim_evidence_links": [
            SimpleNamespace(claim_id=CLAIM1, evidence_card_id=CARD1),
            SimpleNamespace(claim_id=CLAIM2, evidence_card_id=CARD1),
            SimpleNamespace(claim_id=CLAIM2, evidence_card_id=CARD2),
        ],
        "evidence_cards": [
            SimpleNamespace(
                evidence_card_id=CARD1,
                source_id=SOURCE_RECORD_ID,
                locator_refs=[{"block_ordinal": 1, "locator": {"dom": "xpath1"}}],
            ),
            SimpleNamespace(
                evidence_card_id=CARD2,
                source_id=SOURCE_RECORD_ID,
                locator_refs=[],
            ),
        ],
        "reports": [SimpleNamespace(report_payload=dict(REPORT_PAYLOAD))],
    }


def _make_runner(
    monkeypatch,
    *,
    config: EvalExecutionConfig,
    bundle=None,
    plan_payload: dict | None = None,
    final_state: dict | None = None,
    documents=(),
    embedding_provider=None,
    rows=None,
) -> tuple[MultiStageNoAuditVariantRunner, dict]:
    """装配完整 fake 栈的 runner；返回 (runner, holders)。

    `holders`：`bundle`/`calls`/`rehydrator`/`trace`/`captured`/`created_rag`/
    `sessionmaker`。plan-not-supported 测试只需该栈构造完整，run 停在
    `_validate_plan`；full-path 测试继续到 normalize。
    """
    trace: list[str] = []
    if bundle is None:
        bundle, calls = _counting_bundle()
    else:
        bundle, calls = bundle
    rehydrator = _FakeRehydrator(documents=documents)
    parsing = _FakeParsing()
    chunking = _FakeChunking()
    if embedding_provider is None:
        embedding_provider = FakeEmbeddingProvider()
    created_rag = _patch_rag(monkeypatch)
    payload = plan_payload if plan_payload is not None else _plan_payload()
    state = (
        final_state
        if final_state is not None
        else {
            "synthesis_result_id": str(SYNTHESIS_ID),
            "claim_ids": [str(CLAIM1), str(CLAIM2)],
        }
    )
    captured = _patch_pipeline(monkeypatch, trace=trace, plan_payload=payload, final_state=state)
    sessionmaker = _FakeSessionmaker(rows=rows if rows is not None else _normalization_rows())
    runner = MultiStageNoAuditVariantRunner(
        config=config,
        rehydrator=rehydrator,
        parsing_service=parsing,
        chunking_service=chunking,
        sessionmaker=sessionmaker,
        embedding_provider=embedding_provider,
        chroma=None,
        model_factory_bundle=bundle,
    )
    holders = {
        "bundle": bundle,
        "calls": calls,
        "rehydrator": rehydrator,
        "trace": trace,
        "captured": captured,
        "created_rag": created_rag,
        "sessionmaker": sessionmaker,
    }
    return runner, holders


# ---------------------------------------------------------------- model injection gate


@pytest.mark.asyncio
async def test_wrong_provider_identity_assembly_error_zero_factory_calls(monkeypatch) -> None:
    """bundle.provider 与 config.model.provider 不一致 → assembly error，0 factory call。"""
    config = _make_config()
    bundle, calls = _counting_bundle(provider="openai")
    runner, holders = _make_runner(
        monkeypatch, config=config, bundle=(bundle, calls), documents=(_rehydrated_doc(),)
    )
    with pytest.raises(EvalExecutionAssemblyError) as exc:
        await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=None)
    assert "provider" in str(exc.value)
    assert calls == []
    assert holders["rehydrator"].calls == 0


@pytest.mark.asyncio
async def test_wrong_model_id_identity_assembly_error_zero_factory_calls(monkeypatch) -> None:
    """bundle.model_id 与 config.model.model_id 不一致 → assembly error，0 factory call。"""
    config = _make_config()
    bundle, calls = _counting_bundle(model_id="deepseek-reasoner")
    runner, holders = _make_runner(
        monkeypatch, config=config, bundle=(bundle, calls), documents=(_rehydrated_doc(),)
    )
    with pytest.raises(EvalExecutionAssemblyError) as exc:
        await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=None)
    assert "model_id" in str(exc.value)
    assert calls == []
    assert holders["rehydrator"].calls == 0


@pytest.mark.asyncio
async def test_correct_identity_all_factories_same_observer(monkeypatch) -> None:
    """correct identity → full path → 5 个 factory 收到同一个 usage_observer。"""
    config = _make_config()
    collector = EvalLlmUsageCollector(
        execution_spec_fingerprint=SNAP_FP,
        variant_id=EvalVariantId.MULTI_STAGE_NO_AUDIT,
        case_id=CASE_ID,
    )
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=collector)

    called_names = [name for name, _ in holders["calls"]]
    assert sorted(called_names) == ["claim", "draft", "evidence", "planner", "synthesis"]
    observers = {obs for _, obs in holders["calls"]}
    assert observers == {collector}


def _runner_source_path(runner_cls) -> Path:
    """runner 源文件绝对路径（importlib spec，**不依赖 CWD**——CI 从 repo root
    运行 pytest，相对路径 `app/eval/...` 会 FileNotFoundError）。"""
    spec = importlib.util.find_spec(runner_cls.__module__)
    assert spec is not None and spec.origin is not None
    return Path(spec.origin)


def test_runner_imports_no_production_adapter_modules() -> None:
    """AST 断言：runner 不 import 任何生产 adapter 模块（fake factories 必须被实际使用）。"""
    runner_file = _runner_source_path(MultiStageNoAuditVariantRunner)
    tree = ast.parse(runner_file.read_text(encoding="utf-8"), runner_file.name)
    forbidden = (
        "app.research_planning.planner",
        "app.llm.factory",
        "app.stage4.dependencies",
        "app.draft_section.factory",
        "app.analysis.claims.factory",
        "app.analysis.financial.factory",
        "app.analysis.macro.factory",
        "app.analysis.valuation.factory",
        "app.analysis.synthesis.factory",
        "app.eval.variants.multi_stage_no_audit.factory",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert alias.name not in forbidden, f"runner import 生产 adapter 模块: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module not in forbidden, f"runner import 生产 adapter 模块: {node.module}"


# ---------------------------------------------------------------- input closure


@pytest.mark.asyncio
async def test_zero_document_input_unsupported(monkeypatch) -> None:
    config = _make_config()
    snapshot = _snapshot(document_sources=())
    runner, holders = _make_runner(monkeypatch, config=config)
    with pytest.raises(EvalMultiStageNoAuditInputError) as exc:
        await runner.run(
            _case(snapshot), _spec(config), runtime_context=_runtime(), usage_observer=None
        )
    assert exc.value.code == "multi_stage_no_audit_input_not_supported"
    assert holders["calls"] == []
    assert holders["rehydrator"].calls == 0


@pytest.mark.asyncio
async def test_macro_input_unsupported(monkeypatch) -> None:
    config = _make_config()
    snapshot = _snapshot(
        document_sources=(),
        macro_snapshots=(make_macro_ref(snapshot_fingerprint="e" * 64, payload_sha256="f" * 64),),
    )
    runner, holders = _make_runner(monkeypatch, config=config)
    with pytest.raises(EvalMultiStageNoAuditInputError) as exc:
        await runner.run(
            _case(snapshot), _spec(config), runtime_context=_runtime(), usage_observer=None
        )
    assert exc.value.code == "multi_stage_no_audit_input_not_supported"
    assert holders["calls"] == []
    assert holders["rehydrator"].calls == 0


@pytest.mark.asyncio
async def test_structured_input_unsupported(monkeypatch) -> None:
    config = _make_config()
    structured = FrozenStructuredArtifactRef(
        artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
        artifact_id=UID,
        artifact_fingerprint="e" * 64,
        payload_sha256="f" * 64,
    )
    snapshot = _snapshot(document_sources=(), structured_artifacts=(structured,))
    runner, holders = _make_runner(monkeypatch, config=config)
    with pytest.raises(EvalMultiStageNoAuditInputError) as exc:
        await runner.run(
            _case(snapshot), _spec(config), runtime_context=_runtime(), usage_observer=None
        )
    assert exc.value.code == "multi_stage_no_audit_input_not_supported"
    assert holders["calls"] == []
    assert holders["rehydrator"].calls == 0


# ---------------------------------------------------------------- plan closure


@pytest.mark.asyncio
async def test_financial_plan_unsupported(monkeypatch) -> None:
    """planner 产出 financial need → plan_not_supported（需要 live acquisition）。"""
    config = _make_config()
    runner, holders = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        plan_payload=_plan_payload(document=False, financial=True),
    )
    with pytest.raises(EvalMultiStageNoAuditPlanError) as exc:
        await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=None)
    assert exc.value.code == "multi_stage_no_audit_plan_not_supported"
    # 在 plan 校验处 fail-fast：未进入 route / fulfill / stage4。
    assert holders["trace"] == ["task.create", "plan.create_plan"]


@pytest.mark.asyncio
async def test_macro_plan_unsupported(monkeypatch) -> None:
    config = _make_config()
    runner, holders = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        plan_payload=_plan_payload(document=False, macro=True),
    )
    with pytest.raises(EvalMultiStageNoAuditPlanError) as exc:
        await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=None)
    assert exc.value.code == "multi_stage_no_audit_plan_not_supported"


@pytest.mark.asyncio
async def test_valuation_plan_unsupported(monkeypatch) -> None:
    config = _make_config()
    runner, holders = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        plan_payload=_plan_payload(document=False, valuation=True),
    )
    with pytest.raises(EvalMultiStageNoAuditPlanError) as exc:
        await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=None)
    assert exc.value.code == "multi_stage_no_audit_plan_not_supported"


@pytest.mark.asyncio
async def test_empty_document_plan_unsupported(monkeypatch) -> None:
    """plan 有非空 scope/module 但无 document need → fail-fast。"""
    config = _make_config()
    runner, holders = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        plan_payload=_plan_payload(document=False),
    )
    with pytest.raises(EvalMultiStageNoAuditPlanError):
        await runner.run(_case(), _spec(config), runtime_context=_runtime(), usage_observer=None)


@pytest.mark.asyncio
async def test_document_plan_allowed(monkeypatch) -> None:
    """document-only plan → 允许，run 走完整 document executor 路径直到 output。"""
    config = _make_config()
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )
    assert output.variant_id == EvalVariantId.MULTI_STAGE_NO_AUDIT
    assert "plan.route_research_plan" in holders["trace"]


@pytest.mark.asyncio
async def test_event_need_continues_document_executor_path(monkeypatch) -> None:
    """event need 由 DocumentNeedExecutor 从文档满足，不算非 document need → 允许。"""
    config = _make_config()
    runner, holders = _make_runner(
        monkeypatch,
        config=config,
        documents=(_rehydrated_doc(),),
        plan_payload=_plan_payload(document=True, event=True),
    )
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )
    assert output.variant_id == EvalVariantId.MULTI_STAGE_NO_AUDIT
    # event + document 都走 document executor（fulfill 路径完整执行）。
    assert "fulfill.fulfill_research_needs" in holders["trace"]


# ---------------------------------------------------------------- per-attempt isolation


@pytest.mark.asyncio
async def test_collection_isolated_across_attempts(monkeypatch) -> None:
    """same case/config/spec，两个不同 execution_id → 独立 collection + runtime_scope。"""
    config = _make_config()
    uuid_a = UUID("00000000-0000-0000-0000-0000000000a1")
    uuid_b = UUID("00000000-0000-0000-0000-0000000000b1")

    runner_a, holders_a = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    await runner_a.run(
        _case(),
        _spec(config),
        runtime_context=_runtime(execution_id=uuid_a),
        usage_observer=None,
    )
    name_a = holders_a["created_rag"]["vector"].collection_name
    scope_a = holders_a["created_rag"]["vector"].runtime_scope

    runner_b, holders_b = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    await runner_b.run(
        _case(),
        _spec(config),
        runtime_context=_runtime(execution_id=uuid_b),
        usage_observer=None,
    )
    name_b = holders_b["created_rag"]["vector"].collection_name
    scope_b = holders_b["created_rag"]["vector"].runtime_scope

    assert name_a != name_b
    assert name_a.startswith("eval_multi_stage_no_audit_")
    assert scope_a == f"eval:multi_stage_no_audit:{uuid_a.hex}"
    assert scope_b == f"eval:multi_stage_no_audit:{uuid_b.hex}"
    assert scope_a != scope_b


# ---------------------------------------------------------------- stage boundary


@pytest.mark.asyncio
async def test_stage_order_planner_router_fulfillment_stage4_first_draft(monkeypatch) -> None:
    """调用顺序：planner → router → fulfillment → stage4 → first draft，最后 normalize。"""
    config = _make_config()
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )

    trace = holders["trace"]
    expected_prefix = [
        "task.create",
        "plan.create_plan",
        "plan.route_research_plan",
        "fulfill.fulfill_research_needs",
        "stage4.build_graph",
        "stage4.ainvoke",
        "draft.create_or_get_outline",
        "draft.verify_outline_integrity",
        # 2 个 section 各起草一次，然后 Report assembly（之后是 normalize 的 session 读取）。
        "draft.create_or_get_section",
        "draft.create_or_get_section",
        "draft.create_or_get_report",
    ]
    assert trace[: len(expected_prefix)] == expected_prefix
    # 归一化确实走完：产出 normalized output。
    assert output.variant_id == EvalVariantId.MULTI_STAGE_NO_AUDIT
    assert len(output.claims) == 2


def test_no_audit_review_revision_backflow_imports() -> None:
    """静态结构：runner 不 import 生产 audit / review / revision / research_backflow 组件。

    用组件**顶层包前缀**匹配（runner 自身的 `multi_stage_no_audit` 包名含 no_audit，
    不能按子串判断）：audit / review / revision / research_backflow + Stage5 的
    check 节点（report.checks / check_service）。
    """
    runner_file = _runner_source_path(MultiStageNoAuditVariantRunner)
    tree = ast.parse(runner_file.read_text(encoding="utf-8"), runner_file.name)
    forbidden_modules = (
        "app.audit",
        "app.review",
        "app.revision",
        "app.research_backflow",
        "app.report.checks",
        "app.report.check_service",
    )
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                for mod in forbidden_modules:
                    assert not alias.name.startswith(mod), f"runner import 违规模块: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.module is not None
            for mod in forbidden_modules:
                assert not node.module.startswith(mod), f"runner import 违规模块: {node.module}"


# ---------------------------------------------------------------- normalization gate


@pytest.mark.asyncio
async def test_citation_source_fingerprint_is_frozen_content_sha256(monkeypatch) -> None:
    """citation.source_fingerprint == frozen content_sha256（不用 card/chunk/record UUID）。"""
    config = _make_config()
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )
    for citation in output.citations:
        assert citation.source_fingerprint == DOC_SHA
        # 明确不是内部 UUID 身份。
        assert citation.source_fingerprint not in {str(CARD1), str(CARD2), str(SOURCE_RECORD_ID)}
    # citation 稳定 key 前缀 E1/E2...（复用 runner 契约常量，不硬编码字符串）。
    assert [c.citation_id for c in output.citations] == [
        f"{CITATION_KEY_PREFIX}1",
        f"{CITATION_KEY_PREFIX}2",
    ]


@pytest.mark.asyncio
async def test_bidirectional_closure_and_identity(monkeypatch) -> None:
    """claim↔citation 双向闭合 + 无重复 identity，通过 scoring identity 校验。"""
    config = _make_config()
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )

    citation_ids = {c.citation_id for c in output.citations}
    claim_ids = {c.claim_id for c in output.claims}
    for claim in output.claims:
        assert set(claim.citation_ids) <= citation_ids
    for citation in output.citations:
        assert set(citation.claim_ids) <= claim_ids
        for cid in citation.claim_ids:
            claim = next(c for c in output.claims if c.claim_id == cid)
            assert citation.citation_id in claim.citation_ids

    # 生产 scoring 身份校验 + validity 分析必须全绿。
    context = EvalScoringContext(
        execution_spec_fingerprint=SNAP_FP,
        variant_output=output,
        source_snapshot=_case().snapshot,
    )
    verify_variant_output_identity(context)  # duplicate id → raise
    analysis = analyze_citations(context)
    assert len(analysis.valid_citation_ids) == len(output.citations)


@pytest.mark.asyncio
async def test_claims_from_stage4_claim_ids_only(monkeypatch) -> None:
    """归一化只消费本 attempt 的 Stage4 claim_ids；claim_id = 确定性语义 key。"""
    config = _make_config()
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )
    # normalized claim_id 是语义 key（statement/domain/kinds/citation keys 哈希），
    # 跨 attempt 稳定，不是 runtime claim UUID / DB claim_fingerprint。
    from app.eval.variants.multi_stage_no_audit.runner import _semantic_claim_id

    claim1 = holders["sessionmaker"]._rows["claims"][0]
    expected1 = _semantic_claim_id(claim1, ["E1"])
    claim2 = holders["sessionmaker"]._rows["claims"][1]
    expected2 = _semantic_claim_id(claim2, ["E1", "E2"])
    assert {c.claim_id for c in output.claims} == {expected1, expected2}
    assert all(c.claim_type == MULTI_STAGE_NO_AUDIT_CLAIM_TYPE for c in output.claims)
    assert output.final_text == FINAL_TEXT


# ---------------------------------------------------------------- report_artifact_ref gate


@pytest.mark.asyncio
async def test_report_artifact_ref_is_none(monkeypatch) -> None:
    """v1 不输出 runtime Report UUID（fingerprint 会把 artifact_ref 纳入计算）。"""
    config = _make_config()
    runner, holders = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    output = await runner.run(
        _case(), _spec(config), runtime_context=_runtime(), usage_observer=None
    )
    assert output.report_artifact_ref is None


@pytest.mark.asyncio
async def test_semantic_fingerprint_stable_across_attempts(monkeypatch) -> None:
    """语义相同、仅 runtime Report UUID 不同的两个 attempt → fingerprint 相同。"""
    config = _make_config()

    runner_a, _ = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    out_a = await runner_a.run(
        _case(),
        _spec(config),
        runtime_context=_runtime(execution_id=UUID("00000000-0000-0000-0000-0000000000a2")),
        usage_observer=None,
    )
    runner_b, _ = _make_runner(monkeypatch, config=config, documents=(_rehydrated_doc(),))
    out_b = await runner_b.run(
        _case(),
        _spec(config),
        runtime_context=_runtime(execution_id=UUID("00000000-0000-0000-0000-0000000000b2")),
        usage_observer=None,
    )

    # 内容完全相同（最终文本 / claims / citations 均来自同一 fake 数据）。
    assert out_a.final_text == out_b.final_text
    assert compute_variant_output_fingerprint(out_a) == compute_variant_output_fingerprint(out_b)


def test_report_artifact_ref_would_break_semantic_fingerprint() -> None:
    """契约文档：若 v1 输出 runtime UUID 到 report_artifact_ref，同语义 fingerprint 会漂移。"""
    from app.eval.contracts import EvalVariantOutput

    base = EvalVariantOutput(
        variant_id=EvalVariantId.MULTI_STAGE_NO_AUDIT,
        case_id=CASE_ID,
        case_version=1,
        final_text=FINAL_TEXT,
    )
    assert base.report_artifact_ref is None
    fp_none = compute_variant_output_fingerprint(base)
    with_uuid = base.model_copy(update={"report_artifact_ref": str(REPORT_ID)})
    assert with_uuid.report_artifact_ref is not None
    assert compute_variant_output_fingerprint(with_uuid) != fp_none
