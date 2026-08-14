"""Three-variant benchmark experiment runner (stage 7B.1.4D).

对 dataset 的每个 case × 每个 variant 执行 attempt（**每 attempt 全新隔离 PG** +
per-attempt Chroma collection，与 E2E 同一隔离语义），随后：
1. `EvaluationExecutionPersistenceService` 持久化 spec → trial → attempt → usage；
2. deterministic + runtime scoring（citation_validity / citation_coverage /
   latency / tokens / calls / cost / completion），经
   `EvaluationScoringPersistenceService` 落库并 verify（immutable + fingerprint
   replay）；
3. 汇总为 JSON / Markdown / CSV 三类产物（workspace 输出目录）。

模式：
- `fake`（默认，离线）：三路 variant 全部确定性 fake bundle（0 真实 DeepSeek /
  0 网络）——CI / 无 key 环境可复现；
- `real`：生产 DeepSeek adapter（frozen 模型 policy），需 API key，建议
  bounded（case 子集 × 1 trial）。

诚实契约（与 variant v1 契约一致）：
- single_rag / multi_stage_no_audit 遇到 macro / structured 输入
  （`moutai-full` case）→ fail-fast，稳定 error code 记录为 failed attempt
  （不是绕过输入、也不是误报成功）；
- insightforge_full 三 case 全跑（document-only case 用 document-only plan）。
"""

from __future__ import annotations

import asyncio
import json
import os
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
from alembic.config import Config

from alembic import command
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.eval.benchmark.dataset import (
    BENCHMARK_AS_OF,
    BENCHMARK_DATASET_ID,
    BENCHMARK_DATASET_VERSION,
)
from app.eval.benchmark.fakes import (
    create_full_fake_bundle,
    create_multi_stage_fake_bundle,
    create_single_rag_fake_answer,
)
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.contracts import (
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalScoringSpec,
    FrozenModelConfig,
)
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
from app.eval.metrics import MetricValue
from app.eval.persistence.service import EvaluationExecutionPersistenceService
from app.eval.scoring.context import EvalScoringContext
from app.eval.scoring.deterministic import (
    CitationCoverageCalculator,
    CitationValidityCalculator,
    verify_variant_output_identity,
)
from app.eval.scoring.runtime import RUNTIME_CALCULATORS
from app.eval.scoring.service import EvaluationScoringPersistenceService
from app.eval.variants import EvalVariantId
from app.eval.variants.insightforge_full import INSIGHTFORGE_FULL_PROMPT_VERSION
from app.eval.variants.insightforge_full.factory import (
    create_full_model_factory_bundle,
    create_insightforge_full_runner,
)
from app.eval.variants.multi_stage_no_audit import MULTI_STAGE_NO_AUDIT_PROMPT_VERSION
from app.eval.variants.multi_stage_no_audit.factory import (
    create_multi_stage_model_factory_bundle,
    create_multi_stage_no_audit_runner,
)
from app.eval.variants.single_rag import SINGLE_RAG_PROMPT_VERSION
from app.eval.variants.single_rag.factory import create_single_rag_runner
from app.llm.factory import require_llm_credentials
from app.research_planning.contracts import ResearchPlanPayload
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[3]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

# 三路比较矩阵（honest contract）。
_DOCUMENT_ONLY_CASES = frozenset({"moutai-business", "moutai-financial"})
_FULL_INPUT_CASE = "moutai-full"

# moutai-full case 上 single_rag / multi_stage 的**预期**稳定 error code。
_EXPECTED_FAIL_FAST: dict[str, str] = {
    EvalVariantId.SINGLE_RAG.value: "single_rag_input_not_supported",
    EvalVariantId.MULTI_STAGE_NO_AUDIT.value: "multi_stage_no_audit_input_not_supported",
}


@dataclass(frozen=True)
class MetricRecord:
    status: str
    value: str | None
    numerator: str | None = None
    denominator: str | None = None
    reason_code: str | None = None


@dataclass
class AttemptRecord:
    """一次 (case, variant, attempt) 的完整记录（JSON 序列化友好）。"""

    dataset_id: str
    dataset_version: int
    as_of: str
    case_id: str
    variant_id: str
    attempt_no: int
    mode: str
    status: str
    error_code: str | None
    wall_latency_ms: int | None
    execution_id: str
    variant_output_fingerprint: str | None
    usage_components: list[str] = field(default_factory=list)
    usage_call_count: int = 0
    total_tokens: int | None = None
    estimated_cost_usd: str | None = None
    citation_validity: MetricRecord | None = None
    citation_coverage: MetricRecord | None = None
    persisted: bool = False
    expected_fail_fast: bool = False
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict:
        return asdict(self)


# ------------------------------------------------------------------ 计划 payloads


def _document_only_plan(include_annual: bool = True) -> ResearchPlanPayload:
    """document-only 计划（v1 variants 的合法形态；full variant 亦可消费）。"""
    document_needs = [
        {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"},
    ]
    if include_annual:
        document_needs.append(
            {
                "need_code": "annual_docs",
                "purpose": "需要年度报告",
                "source_type": "annual_report",
            }
        )
    return ResearchPlanPayload.model_validate(
        {
            "research_scope": ["business", "risk"],
            "analysis_modules": ["business_event", "risk"],
            "document_needs": document_needs,
            "financial_needs": [],
            "macro_needs": [],
            "event_needs": [],
            "valuation_needs": [],
            "research_focus": ["经营质量"],
        }
    )


def _full_plan() -> ResearchPlanPayload:
    """full 计划：document + financial + macro + valuation 全部 need（moutai-full）。"""
    return ResearchPlanPayload.model_validate(
        {
            "research_scope": ["business", "risk", "financial", "macro", "valuation"],
            "analysis_modules": [
                "business_event",
                "risk",
                "financial",
                "macro",
                "valuation",
            ],
            "document_needs": [
                {
                    "need_code": "news_docs",
                    "purpose": "需要公司新闻",
                    "source_type": "news_article",
                },
                {
                    "need_code": "annual_docs",
                    "purpose": "需要年度报告",
                    "source_type": "annual_report",
                },
            ],
            "financial_needs": [
                {
                    "need_code": "fin_rev_change",
                    "purpose": "需要营收绝对变化",
                    "calculation_code": "absolute_change_cny",
                    "metric_code": "revenue",
                    "period": "2023",
                }
            ],
            "macro_needs": [
                {
                    "need_code": "macro_pop",
                    "purpose": "需要人口宏观数据",
                    "topic_or_indicator": "Population, total",
                }
            ],
            "event_needs": [],
            "valuation_needs": [
                {"need_code": "val_pe", "purpose": "需要市盈率比较", "metric_code": "pe_ttm"}
            ],
            "research_focus": ["经营质量", "估值水平"],
        }
    )


def plan_payload_for(variant_id: EvalVariantId, case_id: str) -> ResearchPlanPayload | None:
    """每个 (variant, case) 的冻结计划（single_rag 无计划）。"""
    if variant_id == EvalVariantId.SINGLE_RAG:
        return None
    if case_id == _FULL_INPUT_CASE and variant_id == EvalVariantId.INSIGHTFORGE_FULL:
        return _full_plan()
    return _document_only_plan()


def make_config(variant_id: EvalVariantId) -> EvalExecutionConfig:
    """冻结执行配置（模型 policy = deepseek:deepseek-v4-flash，与生产一致）。"""
    prompt_version = {
        EvalVariantId.SINGLE_RAG: SINGLE_RAG_PROMPT_VERSION,
        EvalVariantId.MULTI_STAGE_NO_AUDIT: MULTI_STAGE_NO_AUDIT_PROMPT_VERSION,
        EvalVariantId.INSIGHTFORGE_FULL: INSIGHTFORGE_FULL_PROMPT_VERSION,
    }[variant_id]
    return EvalExecutionConfig(
        variant_id=variant_id,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="v1",
        prompt_version=prompt_version,
        retrieval_version="v1",
        pipeline_version="v1",
        retrieval_top_k=5 if variant_id == EvalVariantId.INSIGHTFORGE_FULL else 3,
    )


# ------------------------------------------------------------------ 隔离 target


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


async def _upgrade_head() -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


async def _drop_collection(client, collection_name: str) -> None:
    try:
        await client.delete_collection(collection_name)
    except Exception:  # noqa: BLE001 — 清理失败不影响结果
        pass


@asynccontextmanager
async def _isolated_target(label: str):
    """全新隔离 PG（alembic head）+ 环境覆盖；finally DROP + 恢复 env。"""
    shared_url = get_settings().database_url
    temp_db = f"insightforge_eval_bench_{label}_{uuid4().hex[:10]}"
    temp_url = shared_url.rsplit("/", 1)[0] + f"/{temp_db}"
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{temp_db}"')
    previous = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = temp_url
    get_settings.cache_clear()

    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        await _upgrade_head()
        yield manager.session_factory(), temp_url
    finally:
        await manager.dispose()
        try:
            with _admin_conn("postgres") as conn:
                with conn.cursor() as cur:
                    cur.execute(f'DROP DATABASE IF EXISTS "{temp_db}" WITH (FORCE)')
        finally:
            if previous is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = previous
            get_settings.cache_clear()


# ------------------------------------------------------------------ runner 装配


def _build_runner(
    *,
    variant_id: EvalVariantId,
    config: EvalExecutionConfig,
    plan_payload: ResearchPlanPayload | None,
    loader: EvaluationBundleLoader,
    sessionmaker,
    raw_store: LocalRawArtifactStore,
    chroma: ChromaManager,
    temp_url: str,
    mode: str,
) -> object:
    """按 variant + mode 装配 runner（fake = 确定性离线；real = 生产 adapter）。"""
    settings = get_settings()
    embedding = FakeEmbeddingProvider()

    if variant_id == EvalVariantId.SINGLE_RAG:
        if mode == "real":
            answer_model = _real_single_rag_answer(config)
        else:
            answer_model = create_single_rag_fake_answer(config, observer=None)
        return create_single_rag_runner(
            config=config,
            bundle_loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            embedding_provider=embedding,
            answer_model=answer_model,
        )

    if variant_id == EvalVariantId.MULTI_STAGE_NO_AUDIT:
        if mode == "real":
            bundle = create_multi_stage_model_factory_bundle(config, settings)
        else:
            assert plan_payload is not None
            bundle = create_multi_stage_fake_bundle(config, plan_payload)
        return create_multi_stage_no_audit_runner(
            config=config,
            bundle_loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            embedding_provider=embedding,
            model_factory_bundle=bundle,
        )

    if variant_id == EvalVariantId.INSIGHTFORGE_FULL:
        if mode == "real":
            bundle = create_full_model_factory_bundle(config, settings)
        else:
            assert plan_payload is not None
            bundle = create_full_fake_bundle(config, plan_payload)
        return create_insightforge_full_runner(
            config=config,
            bundle_loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            embedding_provider=embedding,
            model_factory_bundle=bundle,
            checkpoint_uri=to_postgres_connection_uri(temp_url),
        )

    raise ValueError(f"unknown variant: {variant_id}")


def _real_single_rag_answer(config: EvalExecutionConfig):
    """生产 single_rag answer model（ChatDeepSeek + thinking disabled，frozen 配置）。"""
    from langchain_deepseek import ChatDeepSeek

    from app.eval.variants.single_rag.adapter import DeepSeekSingleRagAnswerModel

    settings = get_settings()
    bound = settings.model_copy(
        update={
            "llm_provider": config.model.provider,
            "llm_model": config.model.model_id,
        }
    )
    api_key = bound.deepseek_api_key
    llm = ChatDeepSeek(
        model=bound.llm_model,
        temperature=0.0,
        timeout=bound.llm_timeout_seconds,
        max_retries=bound.llm_max_retries,
        api_key=api_key.get_secret_value() if api_key is not None else None,
        extra_body={"thinking": {"type": "disabled"}},
    )
    return DeepSeekSingleRagAnswerModel(
        llm, provider=config.model.provider, model_id=config.model.model_id
    )


# ------------------------------------------------------------------ scoring


class _RuntimeScoringContext:
    """runtime calculators 需要的轻量上下文（wall_latency / usage / status）。"""

    def __init__(self, *, wall_latency_ms, usage_records, attempt_status) -> None:
        self.wall_latency_ms = wall_latency_ms
        self.usage_records = usage_records
        self.attempt_status = attempt_status


def _metric_record(value: MetricValue) -> MetricRecord:
    return MetricRecord(
        status=value.status.value,
        value=str(value.value) if value.value is not None else None,
        numerator=str(value.numerator) if value.numerator is not None else None,
        denominator=str(value.denominator) if value.denominator is not None else None,
        reason_code=value.reason_code,
    )


def _runtime_metrics(record: AttemptRecord, result) -> None:
    context = _RuntimeScoringContext(
        wall_latency_ms=result.wall_latency_ms,
        usage_records=result.usage_records,
        attempt_status=result.status.value,
    )
    for name, calculator in RUNTIME_CALCULATORS.items():
        metric = calculator.calculate(context)  # type: ignore[arg-type]
        if name.value == "latency_ms":
            record.wall_latency_ms = result.wall_latency_ms
        elif name.value == "llm_call_count":
            record.usage_call_count = int(metric.value) if metric.value is not None else 0
        elif name.value == "total_tokens":
            record.total_tokens = int(metric.value) if metric.value is not None else None
        elif name.value == "estimated_cost":
            record.estimated_cost_usd = (
                str(metric.value) if metric.value is not None else None
            )


# ------------------------------------------------------------------ attempt


async def _run_single_attempt(
    *,
    dataset_root: Path,
    case_id: str,
    variant_id: EvalVariantId,
    attempt_no: int,
    mode: str,
    workdir: Path,
) -> AttemptRecord:
    """一次 (case, variant, attempt)：隔离 PG + per-attempt Chroma + 持久化 + 评分。"""
    config = make_config(variant_id)
    plan_payload = plan_payload_for(variant_id, case_id)
    expected_fail_fast = (
        case_id == _FULL_INPUT_CASE and variant_id.value in _EXPECTED_FAIL_FAST
    )
    record = AttemptRecord(
        dataset_id=BENCHMARK_DATASET_ID,
        dataset_version=BENCHMARK_DATASET_VERSION,
        as_of=BENCHMARK_AS_OF.date().isoformat(),
        case_id=case_id,
        variant_id=variant_id.value,
        attempt_no=attempt_no,
        mode=mode,
        status="pending",
        error_code=None,
        wall_latency_ms=None,
        execution_id="",
        variant_output_fingerprint=None,
        expected_fail_fast=expected_fail_fast,
    )

    loader = EvaluationBundleLoader(dataset_root)
    execution_case = loader.load_execution_case(case_id, 1)
    execution_spec = EvalExecutionSpec(
        case_fingerprint=execution_case.case_fingerprint,
        source_snapshot_fingerprint=compute_source_snapshot_fingerprint(
            execution_case.snapshot
        ),
        execution_config_fingerprint=compute_execution_config_fingerprint(config),
        variant_id=variant_id,
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
    record.execution_id = attempt.execution_id.hex

    raw_root = workdir / "raw" / f"{case_id}_{variant_id.value}_{attempt_no}"
    chroma = ChromaManager(
        host=get_settings().chroma_host,
        port=get_settings().chroma_port,
        ssl=get_settings().chroma_ssl,
        timeout_seconds=get_settings().chroma_timeout_seconds,
    )

    async with _isolated_target(f"{variant_id.value}_{uuid4().hex[:6]}") as (
        sessionmaker,
        temp_url,
    ):
        raw_store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024 * 100)
        runner = _build_runner(
            variant_id=variant_id,
            config=config,
            plan_payload=plan_payload,
            loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            temp_url=temp_url,
            mode=mode,
        )
        collection_name = (
            f"eval_{variant_id.value}_{attempt.execution_id.hex}"
        )
        client = await chroma.get_client()
        result = None
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
        assert result is not None

        record.status = result.status.value
        record.error_code = result.error_code
        record.wall_latency_ms = result.wall_latency_ms
        record.usage_components = sorted(
            {r.component_name for r in result.usage_records}
        )
        if result.variant_output is not None:
            record.variant_output_fingerprint = (
                compute_variant_output_fingerprint(result.variant_output)
            )

        # 1) 执行持久化（spec → trial → attempt → usage；immutable replay）。
        exec_persistence = EvaluationExecutionPersistenceService(sessionmaker)
        spec_id, _ = await exec_persistence.create_or_get_execution_spec(
            execution_spec, config
        )
        _, _ = await exec_persistence.create_or_get_trial(spec_id, trial_spec)
        await exec_persistence.persist_attempt_result(result)

        # 2) 评分：deterministic（仅 success）+ runtime 全部；scoring 落库 + verify。
        record.notes.append("execution_persisted")
        scoring = EvaluationScoringPersistenceService(sessionmaker)
        if result.status == ExecutionAttemptStatus.SUCCESS:
            output = result.variant_output
            assert output is not None
            scoring_context = EvalScoringContext(
                execution_spec_fingerprint=compute_execution_spec_fingerprint(execution_spec),
                variant_output=output,
                source_snapshot=execution_case.snapshot,
            )
            verify_variant_output_identity(scoring_context)
            record.citation_validity = _metric_record(
                CitationValidityCalculator().calculate(scoring_context)
            )
            record.citation_coverage = _metric_record(
                CitationCoverageCalculator().calculate(scoring_context)
            )
            spec = EvalScoringSpec(
                variant_output_fingerprint=result.variant_output_fingerprint,
                human_label_fingerprint=None,
                metric_registry_version=1,
                judge_config_fingerprint=None,
            )
            score_run_id, _ = await scoring.create_or_get_score_run(
                result.execution_id, spec
            )
            values = [
                CitationValidityCalculator().calculate(scoring_context),
                CitationCoverageCalculator().calculate(scoring_context),
            ]
            runtime_context = _RuntimeScoringContext(
                wall_latency_ms=result.wall_latency_ms,
                usage_records=result.usage_records,
                attempt_status="success",
            )
            for _name, calculator in RUNTIME_CALCULATORS.items():
                values.append(calculator.calculate(runtime_context))  # type: ignore[arg-type]
            await scoring.persist_metric_values(score_run_id, tuple(values))
            await scoring.verify_score_run_integrity(score_run_id)
            record.persisted = True
            record.notes.append("scoring_persisted")
        else:
            record.notes.append("failed_attempt_scoring_skipped")

        # 3) attempt 完整性 verify（含 usage 连续校验）。
        await exec_persistence.verify_attempt_integrity(result.execution_id)

    _runtime_metrics(record, result)
    if expected_fail_fast:
        if result.status == ExecutionAttemptStatus.FAILED and (
            result.error_code == _EXPECTED_FAIL_FAST[variant_id.value]
        ):
            record.notes.append("fail_fast_as_expected")
        else:
            record.notes.append(
                f"FAIL_FAST_MISMATCH expected={_EXPECTED_FAIL_FAST[variant_id.value]}"
            )
    return record


# ------------------------------------------------------------------ 实验编排


@dataclass
class BenchmarkExperimentOptions:
    dataset_root: Path
    workdir: Path
    mode: str = "fake"  # fake | real
    cases: tuple[str, ...] = ("moutai-business", "moutai-financial", "moutai-full")
    variants: tuple[EvalVariantId, ...] = tuple(EvalVariantId)
    attempts: int = 1


async def run_benchmark_experiment(options: BenchmarkExperimentOptions) -> dict:
    """运行三路 experiment，产出 JSON + Markdown + CSV 到 workdir。"""
    dataset_root = Path(options.dataset_root)
    workdir = Path(options.workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    if options.mode == "real":
        require_llm_credentials(get_settings())

    records: list[AttemptRecord] = []
    for case_id in options.cases:
        for variant_id in options.variants:
            for attempt_no in range(1, options.attempts + 1):
                record = await _run_single_attempt(
                    dataset_root=dataset_root,
                    case_id=case_id,
                    variant_id=variant_id,
                    attempt_no=attempt_no,
                    mode=options.mode,
                    workdir=workdir,
                )
                records.append(record)
                print(
                    f"[bench] {case_id} / {variant_id.value} / attempt {attempt_no}: "
                    f"{record.status} {record.error_code or ''} "
                    f"({record.wall_latency_ms}ms)".rstrip()
                )

    payload = {
        "dataset_id": BENCHMARK_DATASET_ID,
        "dataset_version": BENCHMARK_DATASET_VERSION,
        "as_of": BENCHMARK_AS_OF.date().isoformat(),
        "mode": options.mode,
        "model": "deepseek:deepseek-v4-flash",
        "generated_at": datetime.now(UTC).isoformat(),
        "attempts": [record.to_json() for record in records],
    }
    _write_outputs(workdir, payload)
    return payload


def _write_outputs(workdir: Path, payload: dict) -> None:
    """results.json + summary.md + summary.csv。"""
    (workdir / "results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (workdir / "summary.md").write_text(
        _render_markdown(payload), encoding="utf-8"
    )
    (workdir / "summary.csv").write_text(_render_csv(payload), encoding="utf-8")


def _render_markdown(payload: dict) -> str:
    lines = [
        "# InsightForge 三路 Variant Benchmark 摘要",
        "",
        f"- dataset: `{payload['dataset_id']}` v{payload['dataset_version']}",
        f"- as_of: {payload['as_of']}",
        f"- mode: `{payload['mode']}`（model: `{payload['model']}`）",
        f"- generated_at: {payload['generated_at']}",
        "",
        (
            "| case | variant | status | error | latency(ms) | calls | tokens | "
            "cost(USD) | citation_validity | citation_coverage | fingerprint(前12) |"
        ),
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for record in payload["attempts"]:
        lines.append(
            (
                "| {case} | {variant} | {status} | {error} | {latency} | {calls} | "
                "{tokens} | {cost} | {validity} | {coverage} | {fp} |"
            ).format(
                case=record["case_id"],
                variant=record["variant_id"],
                status=record["status"],
                error=record["error_code"] or "",
                latency=record["wall_latency_ms"] if record["wall_latency_ms"] is not None else "",
                calls=record["usage_call_count"],
                tokens=record["total_tokens"] if record["total_tokens"] is not None else "",
                cost=record["estimated_cost_usd"] or "",
                validity=_fmt_metric(record["citation_validity"]),
                coverage=_fmt_metric(record["citation_coverage"]),
                fp=(record["variant_output_fingerprint"] or "")[:12],
            )
        )
    return "\n".join(lines) + "\n"


def _fmt_metric(metric: dict | None) -> str:
    if metric is None:
        return ""
    return f"{metric['value'] or ''} ({metric['status']})"


def _render_csv(payload: dict) -> str:
    header = [
        "case_id",
        "variant_id",
        "attempt_no",
        "status",
        "error_code",
        "wall_latency_ms",
        "usage_call_count",
        "total_tokens",
        "estimated_cost_usd",
        "citation_validity_status",
        "citation_validity_value",
        "citation_coverage_status",
        "citation_coverage_value",
        "variant_output_fingerprint",
        "persisted",
        "expected_fail_fast",
    ]
    rows = [",".join(header)]
    for record in payload["attempts"]:
        validity = record["citation_validity"] or {}
        coverage = record["citation_coverage"] or {}
        rows.append(
            ",".join(
                _csv_cell(value)
                for value in (
                    record["case_id"],
                    record["variant_id"],
                    record["attempt_no"],
                    record["status"],
                    record["error_code"] or "",
                    record["wall_latency_ms"] if record["wall_latency_ms"] is not None else "",
                    record["usage_call_count"],
                    record["total_tokens"] if record["total_tokens"] is not None else "",
                    record["estimated_cost_usd"] or "",
                    validity.get("status", ""),
                    validity.get("value", ""),
                    coverage.get("status", ""),
                    coverage.get("value", ""),
                    record["variant_output_fingerprint"] or "",
                    record["persisted"],
                    record["expected_fail_fast"],
                )
            )
        )
    return "\n".join(rows) + "\n"


def _csv_cell(value) -> str:
    text = str(value)
    if "," in text or '"' in text or "\n" in text:
        return '"' + text.replace('"', '""') + '"'
    return text
