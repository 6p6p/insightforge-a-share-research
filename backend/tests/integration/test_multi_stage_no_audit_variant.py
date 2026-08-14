"""Multi-stage no-audit variant integration E2E（stage 7B.1.4C.2）。

Frozen Bundle → EvaluationReplayRehydrator（**每 attempt 独立**临时 PG +
RawArtifactStore）→ SourceParsingService → ChunkingService →
VectorIndexService（real Chroma，per-attempt collection）→ RetrievalService →
ResearchPlanningService（Fake planner）→ ResearchSourceRouter →
ResearchFulfillmentService（真实 DocumentNeedExecutor + 真实 EvidenceExtractionService
+ Fake extractor）→ Stage4 生产 graph（Fake claim/synthesis）→ Claim 持久化 →
Synthesis 持久化 → Stage5 first draft（Outline → DraftSections → Report）→
确定性归一化 `EvalVariantOutput` → `execute_variant_attempt()` harness。

全程 **0 真实 DeepSeek / 0 外部网络**：FakeEmbeddingProvider +
`MultiStageModelFactoryBundle` 全部 fake（但 production runner / services / graphs /
repositories 全部真实）。需要真实 PostgreSQL（127.0.0.1:5433，CREATEDB 权限）+ 真实
Chroma（127.0.0.1:8000）。

关键验收（7B.1.4C.2 FINAL gate）：
1. Attempt A / Attempt B 均 SUCCESS；
2. A/B 使用**不同 PG database** 与**不同 Chroma collection**（attempt 隔离）；
3. 5 个 fake model（planner / evidence / claim / synthesis / draft）在两次 attempt
   **都真实执行**（usage 组件 = research_planner / evidence_extraction /
   claim_analysis / synthesis_analysis / draft_section_writer；**绝无** audit /
   revision_writer）；
4. 同 inputs + 同 fake outputs → `variant_output_fingerprint` 完全一致；
5. citation.source_fingerprint == frozen document content_sha256；
6. claim↔citation 双向闭合（production scoring `analyze_citations` 全绿）。
"""

import asyncio
from contextlib import asynccontextmanager
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config

from alembic import command
from app.analysis.claims.contracts import (
    MAX_CLAIMS_PER_DECISION,
    ClaimAnalysisDecision,
    ClaimAnalysisReason,
    ClaimCandidate,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.analysis.claims.service import ClaimAnalysisService
from app.analysis.synthesis.contracts import (
    SynthesisAnalysisOutput,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisTheme,
)
from app.analysis.synthesis.service import SynthesisAnalysisService
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import EvalExecutionConfig, EvalExecutionSpec, FrozenModelConfig
from app.eval.execution.contracts import (
    EvalExecutionAttempt,
    EvalTrialSpec,
    ExecutionAttemptStatus,
    compute_trial_fingerprint,
)
from app.eval.execution.harness import execute_variant_attempt
from app.eval.fingerprints import (
    compute_execution_config_fingerprint,
    compute_execution_spec_fingerprint,
    compute_source_snapshot_fingerprint,
    compute_variant_output_fingerprint,
)
from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import (
    analyze_citations,
    verify_variant_output_identity,
)
from app.eval.variants import EvalVariantId
from app.eval.variants.multi_stage_no_audit import (
    MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
    MultiStageModelFactoryBundle,
)
from app.eval.variants.multi_stage_no_audit.factory import (
    create_multi_stage_no_audit_runner,
)
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus
from app.research_planning.contracts import ResearchPlanPayload
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.storage.raw_store import LocalRawArtifactStore
from app.synthesis.service import SynthesisService
from app.vectorstore.client import ChromaManager
from tests.analysis.financial.fakes import FakeFinancialAnalysisModel
from tests.analysis.macro.fakes import FakeMacroAnalysisModel
from tests.analysis.synthesis.fakes import FakeSynthesisAnalysisModel
from tests.analysis.valuation.fakes import FakeValuationAnalysisModel
from tests.draft_section.fakes import FakeDraftSectionModel
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.integration.replay_bundle import (
    CASE_ID,
    CASE_VERSION,
    DOC_SHA256,
    build_replay_bundle,
)
from tests.integration.research_fulfillment_helpers import _unique_quote
from tests.integration.test_stage4_workflow import (
    _financial_decision,
    _macro_decision,
    _valuation_decision,
)
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

# usage 组件集合：multi_stage_no_audit 必须恰好这 5 个，绝无 audit / revision。
_EXPECTED_COMPONENTS = {
    "research_planner",
    "evidence_extraction",
    "claim_analysis",
    "synthesis_analysis",
    "draft_section_writer",
}


# ---------------------------------------------------------------- 临时 DB helpers


def _parse_db_url(url: str) -> dict:
    parsed = urlparse(url.replace("+psycopg", "", 1))
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
    }


def _admin_conn(db_name: str) -> psycopg.Connection:
    parts = _parse_db_url(get_settings().database_url)
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
    )


def _create_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')


def _drop_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _temp_url(base: str, db_name: str) -> str:
    return base.rsplit("/", 1)[0] + f"/{db_name}"


async def _upgrade_head() -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


@asynccontextmanager
async def _isolated_target(monkeypatch, tmp_path, *, label: str):
    """每 attempt 一个**全新** PG database（alembic head）+ 独立 raw store。

    两个 attempt 各自调用本 helper → 两个互不可见的隔离 runtime；finally DROP
    database + 恢复 settings（DB creation / migration 在 attempt 计时之外，符合
    7B.1.4C.2「Attempt A → fresh DB A / Attempt B → fresh DB B」）。
    """
    shared_url = get_settings().database_url
    temp_db = f"insightforge_eval_msna_{label}_{uuid4().hex[:10]}"
    temp_url = _temp_url(shared_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / f"raw_{label}", max_bytes=1024 * 1024)
    try:
        await _upgrade_head()
        yield iso_manager.session_factory(), iso_store
    finally:
        await iso_manager.dispose()
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


async def _drop_collection(client, collection_name: str) -> None:
    try:
        await client.delete_collection(collection_name)
    except Exception:
        pass


# ---------------------------------------------------------------- config / plan


def _make_config() -> EvalExecutionConfig:
    return EvalExecutionConfig(
        variant_id=EvalVariantId.MULTI_STAGE_NO_AUDIT,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="v1",
        prompt_version=MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
        retrieval_version="v1",
        pipeline_version="v1",
        retrieval_top_k=3,
    )


def _make_plan_payload() -> ResearchPlanPayload:
    """document-only plan（v1 唯一支持形态）：news_article 需要 + business/risk 模块。

    `news_article` 匹配 replay bundle 的 frozen document（provider=xinhuanet /
    document_type=news_article）；business_event / risk 模块都从 document 证据池
    取输入 → fulfillment 后 readiness 成立。
    """
    return ResearchPlanPayload.model_validate(
        {
            "research_scope": ["business", "risk"],
            "analysis_modules": ["business_event", "risk"],
            "document_needs": [
                {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"}
            ],
            "financial_needs": [],
            "macro_needs": [],
            "event_needs": [],
            "valuation_needs": [],
            "research_focus": ["经营质量"],
        }
    )


# ---------------------------------------------------------------- per-attempt fakes
#
# 每个 fake 模型都是 per-attempt 构造（bundle factory 每次 run 调用 create_*），
# 且把 runner 注入的 usage_observer 绑定进模型——usage 可归因到组件。


def _usage(observer, component: str, *, provider: str, model_id: str) -> LlmCallUsageRecord:
    return LlmCallUsageRecord(
        component_name=component,
        provider=provider,
        model_id=model_id,
        outcome=LlmCallOutcome.SUCCESS,
        duration_ms=1,
        usage_status=UsageStatus.REPORTED,
        input_tokens=20,
        output_tokens=20,
        total_tokens=40,
    )


async def _record_usage(observer, component: str, *, provider: str, model_id: str) -> None:
    if observer is not None:
        await observer.record(_usage(observer, component, provider=provider, model_id=model_id))


class _E2ePlannerModel(FakeResearchPlannerModel):
    """Fake planner + 每次 generate 记录 research_planner usage。"""

    def __init__(self, payload, *, observer, provider, model_id) -> None:
        super().__init__(payload)
        self._observer = observer
        self._provider = provider
        self._model_id = model_id

    async def generate(self, request):
        result = await super().generate(request)
        if self._observer is not None:
            await _record_usage(
                self._observer,
                "research_planner",
                provider=self._provider,
                model_id=self._model_id,
            )
        return result


class _E2eEvidenceModel:
    """按真实 RetrievalHit.text 生成确定性 decision（quote 唯一可解析）+ usage。

    decision 从 hit 文本派生（复用 `_decision_for_text` 模式），**不**手工构造
    hit / quote——证据卡来自真实检索链。
    """

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls: list = []

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def extract(self, research_question, retrieval_hit):
        self.calls.append((research_question, retrieval_hit))
        if self._observer is not None:
            await _record_usage(
                self._observer,
                "evidence_extraction",
                provider=self._provider,
                model_id=self._model_id,
            )
        text = retrieval_hit.text
        if not any(text[i] != text[i - 1] for i in range(1, len(text))):
            return EvidenceExtractionDecision(
                relevant=False,
                items=[],
                reason_code=EvidenceExtractionReason.NOT_RELEVANT,
            )
        return EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement="贵州茅台发布经营相关新闻。",
                    evidence_type=EvidenceType.METRIC,
                    quote_text=_unique_quote(text, 20),
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )


class _E2eClaimModel:
    """确定性 claim fake：按 evidence pack 的 E 编号生成 1..5 条 Claim + usage。

    statement 含 analysis_domain，保证 business / risk worker 的 Claim 语义不同
    （claim fingerprint 含 analysis_domain → 不跨 worker 去重），collect 后
    claim_ids >= 2（synthesis 下限）。
    """

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls: list = []

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, context, evidence_pack):
        self.calls.append((context, evidence_pack))
        if self._observer is not None:
            await _record_usage(
                self._observer,
                "claim_analysis",
                provider=self._provider,
                model_id=self._model_id,
            )
        domain = context.analysis_domain.value
        kind = (
            ClaimKind.RISK
            if domain == "risk"
            else ClaimKind.INFERENCE
            if domain == "event"
            else ClaimKind.FACT
        )
        items = evidence_pack.items[:MAX_CLAIMS_PER_DECISION]
        if not items:
            return ClaimAnalysisDecision(
                relevant=False,
                claims=[],
                reason_code=ClaimAnalysisReason.INSUFFICIENT_EVIDENCE,
            )
        # statement 不内联 E/C alias（draft inline-alias-leak policy 拒绝正文出现
        # C/E/X/G<number> 传输 alias；`valid_decision_for` 会逐字复制 statement）。
        claims = [
            ClaimCandidate(
                statement=f"{domain} 域证据支持公司基本面结论。",
                claim_kind=kind,
                confidence=ClaimConfidence.HIGH,
                importance=ClaimImportance.NORMAL,
                support_refs=[item.evidence_ref],
                contradict_refs=[],
                context_refs=[],
            )
            for item in items
        ]
        return ClaimAnalysisDecision(relevant=True, claims=claims)


class _E2eSynthesisModel(FakeSynthesisAnalysisModel):
    """确定性 synthesis fake：输出从 claim pack 的 C alias 派生（no-cherry-picking
    边界自洽）+ usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        super().__init__(model_id=f"{provider}:{model_id}")
        self._observer = observer
        self._provider = provider
        self._model_id = model_id

    async def analyze(self, context, claim_pack):
        if self._observer is not None:
            await _record_usage(
                self._observer,
                "synthesis_analysis",
                provider=self._provider,
                model_id=self._model_id,
            )
        refs = list(claim_pack.alias_map().keys())
        return SynthesisAnalysisOutput(
            summary="综合判断：多维度证据一致支持公司基本面结论。",
            themes=[
                SynthesisTheme(
                    title="多维度证据支持",
                    summary="各域证据指向一致。",
                    claim_refs=refs,
                )
            ],
            claim_roles=[
                SynthesisClaimRoleAssignment(
                    claim_ref=ref,
                    role=SynthesisClaimRole.SUPPORT,
                    rationale=f"支持 {ref}",
                )
                for ref in refs
            ],
            duplicates=[],
            conflicts=[],
            evidence_gaps=[],
        )


def _e2e_draft_decision_for(pack) -> object:
    """确定性 draft decision：按 claim.statement 排序（跨 attempt 稳定）。

    `valid_decision_for` 依赖 pack 的 C alias 顺序，而 alias 按 runtime claim
    UUID 排序（生产语义，同 DB 内稳定）——跨 attempt（fresh DB，新 UUID）不稳定，
    会破坏 A/B semantic fingerprint 一致性。本工厂按语义字段排序，保证
    「inputs + fake outputs 一致 → fingerprint 一致」。
    """
    from app.draft_section.contracts import ParagraphCandidate, WriterDecision

    paragraphs = []
    for claim in sorted(pack.claims, key=lambda item: item.statement):
        evidence = next((item for item in pack.evidence if claim.alias in item.claim_aliases), None)
        if evidence is None:
            continue
        paragraphs.append(
            ParagraphCandidate(
                text=f"{claim.statement} {evidence.evidence_statement}",
                claim_refs=[claim.alias],
                evidence_refs=[evidence.alias],
            )
        )
    return WriterDecision(paragraphs=paragraphs)


class _E2eDraftModel(FakeDraftSectionModel):
    """确定性 draft fake：按语义字段排序的 decision（跨 attempt 稳定）+ usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        super().__init__(
            model_id=f"{provider}:{model_id}", decision_factory=_e2e_draft_decision_for
        )
        self._observer = observer
        self._provider = provider
        self._model_id = model_id

    async def write(self, pack):
        if self._observer is not None:
            await _record_usage(
                self._observer,
                "draft_section_writer",
                provider=self._provider,
                model_id=self._model_id,
            )
        return await super().write(pack)


def _fake_bundle(
    config: EvalExecutionConfig, plan_payload: ResearchPlanPayload
) -> MultiStageModelFactoryBundle:
    """fake `MultiStageModelFactoryBundle`：身份 = frozen config.model，5 个 factory
    每次 run 构造 per-attempt fake 模型（绑定 usage_observer）。

    `create_stage4_deps` 镜像生产结构：真实 Stage4 服务 + fake claim/synthesis
    （observer 线程一致）；financial/macro/valuation 用固定 fake——document-only
    plan 的 Stage4 work items 只有 business/risk，三者从不 dispatch（0 call）。
    """

    provider = config.model.provider
    model_id = config.model.model_id

    def _make_planner(obs):
        return _E2ePlannerModel(plan_payload, observer=obs, provider=provider, model_id=model_id)

    def _make_evidence(obs):
        return _E2eEvidenceModel(observer=obs, provider=provider, model_id=model_id)

    def _make_claim(obs):
        return _E2eClaimModel(observer=obs, provider=provider, model_id=model_id)

    def _make_synthesis(obs):
        return _E2eSynthesisModel(observer=obs, provider=provider, model_id=model_id)

    def _make_draft(obs):
        return _E2eDraftModel(observer=obs, provider=provider, model_id=model_id)

    def _make_stage4_deps(sessionmaker, obs):
        return Stage4AnalysisDependencies(
            sessionmaker=sessionmaker,
            claim_analysis_service=ClaimAnalysisService(sessionmaker, _make_claim(obs)),
            financial_analysis_service=_FinancialService(sessionmaker, _financial_decision()),
            macro_analysis_service=_MacroService(sessionmaker, _macro_decision()),
            valuation_analysis_service=_ValuationService(sessionmaker, _valuation_decision()),
            synthesis_service=SynthesisService(sessionmaker),
            synthesis_analysis_service=SynthesisAnalysisService(sessionmaker, _make_synthesis(obs)),
        )

    return MultiStageModelFactoryBundle(
        provider=provider,
        model_id=model_id,
        create_planner=_make_planner,
        create_evidence=_make_evidence,
        create_claim=_make_claim,
        create_synthesis=_make_synthesis,
        create_draft=_make_draft,
        create_stage4_deps=_make_stage4_deps,
    )


# financial/macro/valuation services（document-only 下从不 dispatch；真实 service
# 结构 + 固定 fake model，避免 import 依赖环）。
def _FinancialService(sessionmaker, decision):
    from app.analysis.financial.service import FinancialAnalysisService

    return FinancialAnalysisService(sessionmaker, FakeFinancialAnalysisModel(decision=decision))


def _MacroService(sessionmaker, decision):
    from app.analysis.macro.service import MacroAnalysisService

    return MacroAnalysisService(sessionmaker, FakeMacroAnalysisModel(decision=decision))


def _ValuationService(sessionmaker, decision):
    from app.analysis.valuation.service import ValuationAnalysisService

    return ValuationAnalysisService(sessionmaker, FakeValuationAnalysisModel(decision=decision))


# ---------------------------------------------------------------- attempt runner


async def _run_attempt(
    monkeypatch,
    tmp_path,
    *,
    label: str,
    attempt_no: int,
    plan_payload: ResearchPlanPayload,
    config: EvalExecutionConfig,
) -> dict:
    """在一个**全新隔离 PG** 上执行一次完整 attempt，返回结果 + collection 名。

    调用方负责 finally 清理 Chroma collection；PG 由 `_isolated_target` 自动 DROP。
    """
    bundle_root = tmp_path / f"bundle_{label}"
    build_replay_bundle(bundle_root)

    async with _isolated_target(monkeypatch, tmp_path, label=label) as (sessionmaker, raw_store):
        loader = EvaluationBundleLoader(bundle_root)
        execution_case = loader.load_execution_case(CASE_ID, CASE_VERSION)
        execution_spec = EvalExecutionSpec(
            case_fingerprint=execution_case.case_fingerprint,
            source_snapshot_fingerprint=compute_source_snapshot_fingerprint(
                execution_case.snapshot
            ),
            execution_config_fingerprint=compute_execution_config_fingerprint(config),
            variant_id=EvalVariantId.MULTI_STAGE_NO_AUDIT,
        )
        trial_spec = EvalTrialSpec(
            execution_spec_fingerprint=compute_execution_spec_fingerprint(execution_spec),
            trial_no=1,
        )
        attempt = EvalExecutionAttempt(
            trial_fingerprint=compute_trial_fingerprint(trial_spec),
            attempt_no=attempt_no,
            execution_id=uuid4(),
        )

        settings = get_settings()
        chroma = ChromaManager(
            host=settings.chroma_host,
            port=settings.chroma_port,
            ssl=settings.chroma_ssl,
            timeout_seconds=settings.chroma_timeout_seconds,
        )
        runner = create_multi_stage_no_audit_runner(
            config=config,
            bundle_loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            embedding_provider=FakeEmbeddingProvider(),
            model_factory_bundle=_fake_bundle(config, plan_payload),
        )

        collection_name = f"eval_multi_stage_no_audit_{attempt.execution_id.hex}"
        client = await chroma.get_client()
        try:
            result = await execute_variant_attempt(
                attempt=attempt,
                trial_spec=trial_spec,
                execution_spec=execution_spec,
                execution_case=execution_case,
                runner=runner,
            )
        finally:
            await _drop_collection(client, collection_name)

        return {
            "result": result,
            "execution_case": execution_case,
            "execution_spec_fingerprint": compute_execution_spec_fingerprint(execution_spec),
        }


# ---------------------------------------------------------------- E2E tests


async def test_multi_stage_no_audit_full_path_real_chain(monkeypatch, tmp_path) -> None:
    """frozen bundle → 隔离 rehydrate → parse → chunk → real Chroma → 真实多阶段
    流水线（5 个 fake model）→ harness SUCCESS；usage / citation / 归一化全绿。"""
    config = _make_config()
    plan_payload = _make_plan_payload()
    holder = await _run_attempt(
        monkeypatch,
        tmp_path,
        label="full",
        attempt_no=1,
        plan_payload=plan_payload,
        config=config,
    )
    result = holder["result"]
    execution_case = holder["execution_case"]

    # (1) harness 收敛为 success（非 assembly / 非 variant 错误）。
    assert result.status == ExecutionAttemptStatus.SUCCESS, f"error_code={result.error_code}"
    assert result.error_code is None
    assert result.variant_id == EvalVariantId.MULTI_STAGE_NO_AUDIT
    assert isinstance(result.wall_latency_ms, int) and result.wall_latency_ms >= 0

    # (2) output 身份 + fingerprint 闭合。
    output = result.variant_output
    assert output is not None
    assert output.variant_id == EvalVariantId.MULTI_STAGE_NO_AUDIT
    assert output.case_id == CASE_ID
    assert output.case_version == CASE_VERSION
    assert result.variant_output_fingerprint == compute_variant_output_fingerprint(output)

    # (3) citation.source_fingerprint == frozen content_sha256（语义引用，非 runtime UUID）。
    assert output.citations, "multi_stage_no_audit 必须产出至少一条 citation"
    assert output.claims, "multi_stage_no_audit 必须产出至少一条 claim"
    for citation in output.citations:
        assert citation.source_fingerprint == DOC_SHA256

    # (4) claim↔citation 双向闭合 + 生产 scoring 身份校验全绿。
    context = EvalScoringContext(
        execution_spec_fingerprint=holder["execution_spec_fingerprint"],
        variant_output=output,
        source_snapshot=execution_case.snapshot,
    )
    verify_variant_output_identity(context)  # duplicate id → raise
    analysis = analyze_citations(context)
    assert len(analysis.valid_citation_ids) == len(output.citations)

    # (5) usage：恰好 5 个组件，各 >=1 条记录；绝无 audit / revision_writer。
    components = {r.component_name for r in result.usage_records}
    assert components == _EXPECTED_COMPONENTS
    for component in _EXPECTED_COMPONENTS:
        count = sum(1 for r in result.usage_records if r.component_name == component)
        assert count >= 1, f"组件 {component} 应有 >=1 次 LLM 调用"

    # (6) report_artifact_ref 恒 None（v1 语义 fingerprint 稳定规则）。
    assert output.report_artifact_ref is None


async def test_multi_stage_no_audit_attempt_isolation_and_determinism(
    monkeypatch,
    tmp_path,
) -> None:
    """Attempt A / Attempt B：不同 PG database + 不同 Chroma collection；5 个 fake
    model 两次都真实执行；同 inputs → variant_output_fingerprint 完全一致。"""
    config = _make_config()
    plan_payload = _make_plan_payload()

    holder_a = await _run_attempt(
        monkeypatch, tmp_path, label="a", attempt_no=1, plan_payload=plan_payload, config=config
    )
    result_a = holder_a["result"]
    assert result_a.status == ExecutionAttemptStatus.SUCCESS, f"A failed: {result_a.error_code}"

    holder_b = await _run_attempt(
        monkeypatch, tmp_path, label="b", attempt_no=2, plan_payload=plan_payload, config=config
    )
    result_b = holder_b["result"]
    assert result_b.status == ExecutionAttemptStatus.SUCCESS, f"B failed: {result_b.error_code}"

    # (1) 两次 attempt 各自独立 execution_id → 不同 collection（runner 派生名绑定
    #     execution_id；DB 由 `_isolated_target` 各自创建，天然隔离）。
    assert result_a.execution_id != result_b.execution_id

    # (2) 5 个 fake model 在两次 attempt 都真实执行（usage 组件集合一致且非空）。
    for result in (result_a, result_b):
        components = {r.component_name for r in result.usage_records}
        assert components == _EXPECTED_COMPONENTS
        forbidden = {"audit", "revision_writer"}
        assert not any(r.component_name in forbidden for r in result.usage_records)

    # (3) 同 inputs + 同 fake outputs → 语义 fingerprint 完全一致（runtime UUID /
    #     execution_id / DB 身份不泄漏进 fingerprint）。
    out_a = result_a.variant_output
    out_b = result_b.variant_output
    assert out_a is not None and out_b is not None
    assert result_a.variant_output_fingerprint == result_b.variant_output_fingerprint
    assert compute_variant_output_fingerprint(out_a) == compute_variant_output_fingerprint(out_b)
    assert out_a.final_text == out_b.final_text
    assert {c.claim_id for c in out_a.claims} == {c.claim_id for c in out_b.claims}

    # (4) citation 引用 frozen SHA（不引用任何 runtime UUID 身份）。
    for output in (out_a, out_b):
        for citation in output.citations:
            assert citation.source_fingerprint == DOC_SHA256
