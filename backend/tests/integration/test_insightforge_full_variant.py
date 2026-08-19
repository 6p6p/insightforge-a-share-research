"""InsightForge full variant integration E2E（stage 7B.1.4C.4）。

Frozen Bundle（document news_article + annual_report + macro + financial
observations + valuation observation + comparison）→ 每 attempt 独立隔离 PG →
rehydrate → parse/chunk/index → **生产顶层编排**（真实 ResearchOrchestrationRunner
+ 真实 graph + PG Checkpointer）→ 真实 Fulfillment（document/financial/macro/
valuation executors）+ **inline structured remap** → Stage4 → Stage5（deterministic
checks → semantic Audit → Review Routing → Revision / Research Backflow）→
`execute_variant_attempt()` harness。

全程 0 真实 DeepSeek / 0 外部网络：FakeEmbeddingProvider + fake
`FullModelFactoryBundle`（production runner / services / graphs / repositories /
checkpointer 全部真实）。需要真实 PostgreSQL（127.0.0.1:5433，CREATEDB）+ 真实
Chroma（127.0.0.1:8002）。

覆盖：
1. happy path：Audit 真实执行（usage 含 audit），revision 不需要时不执行；
2. revision：audit 判定 rewrite → Revision 真实执行（usage 含 revision_writer）；
3. backflow：audit 判定 research → 真实补充检索 → Stage4 a2 → Stage5 a2；
4. human policy：audit 判定 human_review → evaluation 自动 approve → 完成。
"""

import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.eval.bundle.loader import EvaluationBundleLoader
from app.eval.bundle.writer import EvaluationBundleWriter
from app.eval.contracts import (
    EvalExecutionConfig,
    EvalExecutionSpec,
    FrozenModelConfig,
    StructuredArtifactType,
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
from app.eval.materialization import (
    EvalCaseMaterializationSpec,
    EvaluationSnapshotMaterializer,
    StructuredArtifactSelection,
)
from app.eval.variants import EvalVariantId
from app.eval.variants.insightforge_full import (
    INSIGHTFORGE_FULL_PROMPT_VERSION,
    FullModelFactoryBundle,
)
from app.eval.variants.insightforge_full.factory import create_insightforge_full_runner
from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.evidence.extractor.contracts import (
    EvidenceExtractionDecision,
    EvidenceExtractionItem,
    EvidenceExtractionReason,
)
from app.llm.instrumentation import LlmCallOutcome, LlmCallUsageRecord, UsageStatus
from app.research_planning.contracts import ResearchPlanPayload
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.comparison_service import RelativeValuationComparisonService
from app.valuation.contracts import ComparisonDraft
from app.vectorstore.client import ChromaManager
from tests.embedding.fakes import FakeEmbeddingProvider
from tests.integration.research_fulfillment_helpers import _unique_quote
from tests.integration.test_eval_snapshot_materializer import (
    _ANALYSIS_AS_OF,
    _QUESTION,
)
from tests.integration.test_macro_evidence_service import _seed_macro_chain
from tests.integration.test_valuation_comparison_service import (
    _seed_company,
    _seed_observation,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

# Full variant 每次成功 attempt 必须出现的 usage 组件（audit 必现）。
_FULL_COMPONENTS = {
    "research_planner",
    "evidence_extraction",
    "claim_analysis",
    "financial_analysis",
    "macro_analysis",
    "valuation_analysis",
    "synthesis_analysis",
    "draft_section_writer",
    "audit",
}
_REVISION_COMPONENT = "revision_writer"


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


async def _upgrade_head() -> None:
    cfg = Config(str(ALEMBIC_INI))
    await asyncio.to_thread(command.upgrade, cfg, "head")


@asynccontextmanager
async def _isolated_target(monkeypatch, tmp_path, *, label: str):
    shared_url = get_settings().database_url
    temp_db = f"insightforge_eval_full_{label}_{uuid4().hex[:10]}"
    temp_url = shared_url.rsplit("/", 1)[0] + f"/{temp_db}"
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()

    iso_manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    iso_store = LocalRawArtifactStore(root=tmp_path / f"raw_full_{label}", max_bytes=1024 * 1024)
    try:
        await _upgrade_head()
        yield iso_manager.session_factory(), iso_store, temp_url
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


# ---------------------------------------------------------------- source PG seed


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
        await session.execute(text("DELETE FROM chunk_vector_indexes"))
        await session.execute(text("DELETE FROM document_chunks"))
        await session.execute(text("DELETE FROM chunk_sets"))
        await session.execute(text("DELETE FROM parsed_source_blocks"))
        await session.execute(text("DELETE FROM parsed_sources"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw_full_src", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    from app.services.source_registry_service import SourceRegistryService
    from tests.integration.research_fulfillment_helpers import _seed_world_bank_provider
    from tests.integration.test_valuation_comparison_service import _seed_company

    await SourceRegistryService(sessionmaker).seed_defaults()
    await _seed_world_bank_provider(sessionmaker)
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _seed_document(
    env: dict, company_id, *, document_type: str, body_text: str, provider_key: str = "xinhuanet"
) -> dict:
    """归档 HTML + SourceRecord（document_type / provider_key 可参数化）。

    annual_report 必须挂 sse provider（路由把 company_announcement 能力分给 sse；
    xinhuanet 只有 news_article 能力）。
    """
    from app.db.models.raw_artifact import RawArtifactModel
    from app.db.models.source_record import SourceRecordModel
    from app.repositories.raw_artifact_repository import RawArtifactRepository
    from app.repositories.source_record_repository import SourceRecordRepository

    html = (
        "<html><head><title>研究材料</title></head><body><article>"
        f"<p>{body_text}</p></article></body></html>"
    ).encode()
    stored = env["raw_store"].put_html_bytes(html)
    capabilities = (
        ["news_article", "document_download"]
        if provider_key == "xinhuanet"
        else ["company_announcement", "document_download"]
    )
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=company_id,
            provider_key=provider_key,
            artifact_id=artifact.artifact_id,
            document_type=document_type,
            title="研究材料",
            published_at=datetime(2026, 7, 1, tzinfo=UTC),
            reporting_period_end=None,
            source_url="https://www.xinhuanet.com/2026/0701/0001.htm",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=capabilities,
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    return {"source_id": source_id, "content_sha256": stored.content_sha256}


async def _seed_financial_observation(
    env: dict, company_id, *, period_end: date, value_text: str
) -> dict:
    """真实 document → evidence → FinancialMetricObservation 链（period 参数化）。"""
    from app.db.models.raw_artifact import RawArtifactModel
    from app.db.models.source_record import SourceRecordModel
    from app.evidence.contracts import EvidenceCardDraft
    from app.financial.contracts import (
        FinancialMetricDraft,
        MetricCode,
        RawUnit,
        StatementScope,
    )
    from app.financial.service import FinancialMetricService
    from app.repositories.document_chunk_repository import DocumentChunkRepository
    from app.repositories.raw_artifact_repository import RawArtifactRepository
    from app.repositories.source_record_repository import SourceRecordRepository
    from app.services.chunking_service import ChunkingService
    from app.services.evidence_card_service import EvidenceCardService
    from app.services.source_parsing_service import SourceParsingService

    year = period_end.year
    html = (
        "<html><head><title>财务披露</title></head><body><article>"
        f"<p>{year}年贵州茅台营业收入{value_text}万元，经营稳健。</p></article></body></html>"
    ).encode()
    stored = env["raw_store"].put_html_bytes(html)
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=company_id,
            provider_key="xinhuanet",
            artifact_id=artifact.artifact_id,
            document_type="news_article",
            title="财务披露",
            published_at=datetime(2026, 7, 1, tzinfo=UTC),
            reporting_period_end=None,
            source_url="https://www.xinhuanet.com/2026/0701/0002.htm",
            acquisition_method="public_html",
            status="available",
            authority_tier_snapshot=3,
            critical_claim_eligible_snapshot=False,
            provider_capabilities_snapshot=["news_article"],
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        source_id = record.source_id
    parsed = await SourceParsingService(env["sessionmaker"], env["raw_store"]).parse_source(
        source_id
    )
    chunked = await ChunkingService(env["sessionmaker"]).chunk_parsed_source(
        parsed.parsed_source_id
    )
    async with env["sessionmaker"]() as session:
        chunks = await DocumentChunkRepository(session).list_for_chunk_set(chunked.chunk_set_id)
    assert chunks, "financial seed must produce chunks"
    chunk = next(c for c in chunks if value_text in c.text)
    idx = chunk.text.index(value_text)
    card = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement=f"{year}年营业收入为{value_text}万元",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=idx,
            quote_end=idx + len(value_text),
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    obs = await FinancialMetricService(env["sessionmaker"]).create_observation(
        FinancialMetricDraft(
            company_id=company_id,
            source_evidence_card_id=card.evidence_card_id,
            metric_code=MetricCode.REVENUE,
            statement_scope=StatementScope.CONSOLIDATED,
            period_start=date(year, 1, 1),
            period_end=date(year, 12, 31),
            source_value_text=value_text,
            raw_unit=RawUnit.TEN_THOUSAND_YUAN,
        )
    )
    return {
        "metric_observation_id": obs.metric_observation_id,
        "metric_fingerprint": obs.metric_fingerprint,
    }


async def _build_full_bundle(
    env: dict, bundle_root: Path, monkeypatch, *, include_annual_doc: bool = True
) -> dict:
    """真实 PG → full frozen bundle（document + macro + financial + valuation）。

    `include_annual_doc=False`（backflow 场景）：不 seed annual_report 源——
    backflow 的检索只面对 news chunks；round-1 top_k=5 只提取部分 chunk，backflow
    命中剩余 chunk → 新 EvidenceCard → 进度成立（ref-aware claims 随新卡变化）。
    """
    # documents：news_article（document need；16 段 × ~200 字符 → ~8 chunks，
    # round-1 top_k=5 只提取部分，backflow 命中剩余 chunk → 新 EvidenceCard →
    # 进度；段落主题差异大 → 不同 query 命中不同 chunk）。
    news_topic_sentences = [
        "贵州茅台酱香型白酒产能扩张，基酒产量稳步提升，库存结构持续优化。",
        "白酒行业消费升级趋势明显，高端产品收入占比持续提升，渠道库存合理。",
        "公司直营渠道收入快速增长，经销商体系保持稳定，数字化营销成效显著。",
        "原材料采购成本基本平稳，包材价格温和上涨，毛利率保持高位运行。",
        "系列酒产品线持续丰富，市场推广力度加大，区域市场渗透率提升。",
        "海外市场拓展稳步推进，国际业务收入占比小幅提升，品牌影响力增强。",
        "公司研发投入持续增加，生产智能化改造推进，产品质量保持稳定。",
        "电商平台销售增长较快，年轻消费群体占比提升，消费场景日益多元。",
        "公司分红政策稳定，股东回报持续提升，现金流状况保持健康水平。",
        "行业竞争格局总体稳定，公司核心产品竞争力突出，市场份额领先同业。",
        "公司管理层保持稳定，经营策略连贯，治理结构持续完善，信息披露规范。",
        "宏观经济环境温和复苏，消费需求逐步回暖，白酒消费场景恢复增长。",
        "公司生产基地区位优势明显，酿造工艺传承创新，产品品质稳定可靠。",
        "公司销售费用率稳中有降，费用投放效率提升，利润结构持续改善。",
        "公司预收款项保持稳定，经销商打款积极，渠道信心充足，动销良好。",
        "公司吨酒价格稳步上行，产品结构升级带动均价提升，盈利能力增强。",
    ]
    news_paragraphs = [
        f"第{i}段。{sentence}" + "经营数据符合预期，管理层在业绩说明会上作出详细说明。" * 3
        for i, sentence in enumerate(news_topic_sentences, start=1)
    ]
    news_body = "".join(news_paragraphs)
    news = await _seed_document(
        env, env["company_id"], document_type="news_article", body_text=news_body
    )
    annual = None
    if include_annual_doc:
        annual = await _seed_document(
            env,
            env["company_id"],
            document_type="annual_report",
            body_text="2024年年度报告：营业收入保持增长，经营稳健",
            provider_key="sse",
        )
    # sse-news 证据源：news_article 类型 + sse provider。round-1 的 news 路由只
    # 分给 xinhuanet → 不提取；backflow 无 provider 过滤且检索 `document_types`
    # 覆盖 news_article → 命中。正文 = backflow 基础 query 原文（section context
    # 前缀 + research question + need desc）→ 向量距离 0 → 必然 top-1（确定性
    # 进度：新 EvidenceCard → pool（news_article）纳入 → ref-aware claims 变化）。
    await _seed_document(
        env,
        env["company_id"],
        document_type="news_article",
        body_text=(
            "经营质量综合评估：分析贵州茅台的经营质量、主要风险和估值水平。（核实证据支持）"
        ),
        provider_key="sse",
    )
    # macro snapshot（world_bank population，MockTransport 0 网络）。
    macro = await _seed_macro_chain(env, monkeypatch)
    # financial observations：2024 / 2023 营收（absolute_change 需要 current+baseline）。
    fin_2024 = await _seed_financial_observation(
        env, env["company_id"], period_end=date(2024, 12, 31), value_text="123,456"
    )
    fin_2023 = await _seed_financial_observation(
        env, env["company_id"], period_end=date(2023, 12, 31), value_text="100,000"
    )
    # valuation：target observation + comparison（3 peers）。
    target = await _seed_observation(env, env["company_id"], "15.3")
    peer_obs_ids = []
    for i, value in enumerate(["14.2", "15.0", "16.0"]):
        peer_company = await _seed_company(env["sessionmaker"], f"6005{2 + i:02d}")
        peer_obs_ids.append(
            (await _seed_observation(env, peer_company, value))["valuation_observation_id"]
        )
    comparison = await RelativeValuationComparisonService(env["sessionmaker"]).create_comparison(
        ComparisonDraft(
            target_company_id=env["company_id"],
            target_observation_id=target["valuation_observation_id"],
            peer_observation_ids=tuple(peer_obs_ids),
            analysis_as_of=date(2026, 8, 10),
        )
    )

    # 全部 target 公司 document source ids。
    async with env["sessionmaker"]() as session:
        rows = (
            await session.execute(
                text("SELECT source_id FROM source_records WHERE company_id = :cid").bindparams(
                    cid=env["company_id"]
                )
            )
        ).all()
    document_source_ids = tuple(row[0] for row in rows)
    assert document_source_ids

    spec = EvalCaseMaterializationSpec(
        case_id="full-case",
        case_version=1,
        company_id=env["company_id"],
        security_code="600519",
        research_question=_QUESTION,
        analysis_as_of=_ANALYSIS_AS_OF,
        tags=("full",),
        document_source_ids=document_source_ids,
        macro_snapshot_ids=(macro["snapshot_id"],),
        structured_artifacts=(
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                artifact_id=fin_2024["metric_observation_id"],
            ),
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION,
                artifact_id=fin_2023["metric_observation_id"],
            ),
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION,
                artifact_id=target["valuation_observation_id"],
            ),
            StructuredArtifactSelection(
                artifact_type=StructuredArtifactType.RELATIVE_VALUATION_COMPARISON,
                artifact_id=comparison.comparison_id,
            ),
        ),
    )
    materializer = EvaluationSnapshotMaterializer(env["sessionmaker"], env["raw_store"])
    materialized = await materializer.materialize_case(spec)
    writer = EvaluationBundleWriter(bundle_root)
    EvaluationSnapshotMaterializer.write_materialized(materialized, writer)
    return {"materialized": materialized, "news": news, "annual": annual}


# ---------------------------------------------------------------- plan payload


def _unique_quote_with_marker(text: str, quote_len: int = 40) -> str:
    """quote 从段落唯一标记「第N段」开始（chunk 内唯一），无标记回退通用实现。"""
    marker = text.find("第")
    if marker >= 0:
        return text[marker : marker + quote_len]
    return _unique_quote(text, 20)


def _make_full_plan_payload(*, include_annual: bool = True) -> ResearchPlanPayload:
    """full plan：document（news + annual）+ financial + macro + valuation 全部 need。

    `include_annual=False`（backflow 场景）：annual_report source 在 round-1 不
    提取 → backflow 的真实检索命中其 chunk → 新 EvidenceCard → 进度成立。
    """
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
            "research_scope": ["business", "risk", "financial", "macro", "valuation"],
            "analysis_modules": [
                "business_event",
                "risk",
                "financial",
                "macro",
                "valuation",
            ],
            "document_needs": document_needs,
            "financial_needs": [
                {
                    "need_code": "fin_rev_change",
                    "purpose": "需要营收绝对变化",
                    "calculation_code": "absolute_change_cny",
                    "metric_code": "revenue",
                    "period": "2024",
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


def _make_config() -> EvalExecutionConfig:
    return EvalExecutionConfig(
        variant_id=EvalVariantId.INSIGHTFORGE_FULL,
        model=FrozenModelConfig(
            provider="deepseek",
            model_id="deepseek-v4-flash",
            thinking_enabled=False,
            temperature=Decimal("0"),
            structured_output=True,
        ),
        variant_version="v1",
        prompt_version=INSIGHTFORGE_FULL_PROMPT_VERSION,
        retrieval_version="v1",
        pipeline_version="v1",
        retrieval_top_k=5,
    )


# ---------------------------------------------------------------- per-attempt fakes


def _usage(component: str, *, provider: str, model_id: str) -> LlmCallUsageRecord:
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


async def _record(observer, component: str, *, provider: str, model_id: str) -> None:
    if observer is not None:
        await observer.record(_usage(component, provider=provider, model_id=model_id))


class _E2ePlannerModel:
    def __init__(self, payload, *, observer, provider, model_id) -> None:
        self._payload = payload
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def generate(self, request):
        self.calls += 1
        await _record(
            self._observer, "research_planner", provider=self._provider, model_id=self._model_id
        )
        return self._payload


class _E2eEvidenceModel:
    """按真实 RetrievalHit.text 生成确定性 decision（quote 唯一可解析）+ usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def extract(self, research_question, retrieval_hit):
        self.calls += 1
        await _record(
            self._observer, "evidence_extraction", provider=self._provider, model_id=self._model_id
        )
        text_value = retrieval_hit.text
        if not any(text_value[i] != text_value[i - 1] for i in range(1, len(text_value))):
            return EvidenceExtractionDecision(
                relevant=False, items=[], reason_code=EvidenceExtractionReason.NOT_RELEVANT
            )
        return EvidenceExtractionDecision(
            relevant=True,
            items=[
                EvidenceExtractionItem(
                    evidence_statement="公司发布经营相关材料。",
                    evidence_type=EvidenceType.METRIC,
                    quote_text=_unique_quote_with_marker(text_value),
                    confidence=EvidenceConfidence.HIGH,
                )
            ],
        )


class _E2eClaimModel:
    """Ref-aware claim fake：引用 evidence pack **全部** refs + usage。

    backflow 加入新 EvidenceCard → 新 support ref → 新 claim fingerprint →
    新 synthesis run（确定性，不依赖 UUID 排序——镜像生产 backflow E2E 的
    `_RefAwareClaimModel` 模式）。
    """

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, context, evidence_pack):
        await _record(
            self._observer, "claim_analysis", provider=self._provider, model_id=self._model_id
        )
        from app.analysis.claims.contracts import (
            ClaimAnalysisDecision,
            ClaimAnalysisReason,
            ClaimCandidate,
            ClaimConfidence,
            ClaimImportance,
            ClaimKind,
        )

        domain = context.analysis_domain.value
        kind = (
            ClaimKind.RISK
            if domain == "risk"
            else ClaimKind.INFERENCE
            if domain == "event"
            else ClaimKind.FACT
        )
        items = evidence_pack.items
        if not items:
            return ClaimAnalysisDecision(
                relevant=False,
                claims=[],
                reason_code=ClaimAnalysisReason.INSUFFICIENT_EVIDENCE,
            )
        claims = [
            ClaimCandidate(
                statement=f"{domain} 域证据支持公司基本面结论。",
                claim_kind=kind,
                confidence=ClaimConfidence.HIGH,
                importance=ClaimImportance.NORMAL,
                support_refs=[item.evidence_ref for item in items],
                contradict_refs=[],
                context_refs=[],
            )
        ]
        return ClaimAnalysisDecision(relevant=True, claims=claims)


class _RecordingFixedModel:
    """包装固定 decision 的 fake（financial / macro / valuation 分析，usage 记录）。"""

    def __init__(self, decision, component: str, *, observer, provider, model_id) -> None:
        self._decision = decision
        self._component = component
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, *args, **kwargs):
        self.calls += 1
        await _record(
            self._observer, self._component, provider=self._provider, model_id=self._model_id
        )
        return self._decision


class _E2eSynthesisModel:
    """确定性 synthesis fake：输出从 claim pack 的 C alias 派生 + usage。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def analyze(self, context, claim_pack):
        await _record(
            self._observer, "synthesis_analysis", provider=self._provider, model_id=self._model_id
        )
        from app.analysis.synthesis.contracts import (
            SynthesisAnalysisOutput,
            SynthesisClaimRole,
            SynthesisClaimRoleAssignment,
            SynthesisTheme,
        )

        refs = list(claim_pack.alias_map().keys())
        # 拆多个 theme（每个 <=5 refs）→ outline 每个 section 的 claim 集合受控，
        # 避免单 section 段落数超过 WriterDecision 上限（1..10）。首个 theme 标题
        # 固定为「经营质量综合评估」：backflow 的基础检索 query 带 section context
        # 前缀，sse-news 证据文档用同文本 → 距离 0 → 必然命中（确定性 backflow
        # 进度，见 `_build_full_bundle`）。
        themes = [
            SynthesisTheme(
                title="经营质量综合评估" if i == 0 else f"证据主题 {i}",
                summary="各域证据指向一致。",
                claim_refs=refs[i : i + 5],
            )
            for i in range(0, len(refs), 5)
        ]
        return SynthesisAnalysisOutput(
            summary="综合判断：多维度证据一致支持公司基本面结论。",
            themes=themes,
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

    段落数上限 10（WriterDecision 契约 1..10）：只写前 10 条 claim。
    """
    from app.draft_section.contracts import ParagraphCandidate, WriterDecision

    paragraphs = []
    for claim in sorted(pack.claims, key=lambda item: item.statement)[:10]:
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


class _E2eDraftModel:
    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def write(self, pack, correction_hint: str | None = None):
        await _record(
            self._observer, "draft_section_writer", provider=self._provider, model_id=self._model_id
        )
        return _e2e_draft_decision_for(pack)


class _E2eAuditModel:
    """确定性 audit fake：decision_factory（sequenced）+ usage 记录。"""

    def __init__(self, decision_factory, *, observer, provider, model_id) -> None:
        self._decision_factory = decision_factory
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def audit(self, pack, hint: str | None = None):
        self.calls += 1
        await _record(self._observer, "audit", provider=self._provider, model_id=self._model_id)
        return self._decision_factory(pack)


class _E2eRevisionModel:
    """确定性 revision fake：revision_decision_for + usage 记录。"""

    def __init__(self, *, observer, provider, model_id) -> None:
        self._observer = observer
        self._provider = provider
        self._model_id = model_id
        self.calls = 0

    @property
    def model_id(self) -> str:
        return f"{self._provider}:{self._model_id}"

    async def rewrite(self, pack):
        self.calls += 1
        await _record(
            self._observer, "revision_writer", provider=self._provider, model_id=self._model_id
        )
        from tests.revision.fakes import revision_decision_for

        return revision_decision_for(pack)


def _fake_bundle(
    config: EvalExecutionConfig,
    plan_payload: ResearchPlanPayload,
    audit_factories,
) -> FullModelFactoryBundle:
    """fake `FullModelFactoryBundle`：身份 = frozen config.model；10 个 factory
    每次 run 构造 per-attempt fake 模型（绑定 usage_observer）。

    `audit_factories`：callable 列表，audit fake 按调用次序取（最后重复），用于
    构造 pass / rewrite→pass / research→pass / human_review→pass 场景。
    """
    from app.analysis.claims.service import ClaimAnalysisService
    from app.analysis.financial.service import FinancialAnalysisService
    from app.analysis.macro.service import MacroAnalysisService
    from app.analysis.synthesis.service import SynthesisAnalysisService
    from app.analysis.valuation.service import ValuationAnalysisService
    from app.audit.service import ReportAuditService
    from app.draft_section.service import DraftSectionService
    from app.report.check_service import ReportCheckService
    from app.report.service import ReportService
    from app.report_outline.service import ReportOutlineService
    from app.research_backflow.service import ResearchBackflowService
    from app.review.service import ReviewActionService
    from app.revision.service import RevisionService
    from app.stage4.dependencies import Stage4AnalysisDependencies
    from app.stage5.dependencies import Stage5WorkflowDependencies
    from app.synthesis.service import SynthesisService
    from tests.integration.test_stage4_workflow import (
        _financial_decision,
        _macro_decision,
        _valuation_decision,
    )

    provider = config.model.provider
    model_id = config.model.model_id

    def _make_audit(obs):
        return _E2eAuditModel(
            _SequencedFactories(audit_factories),
            observer=obs,
            provider=provider,
            model_id=model_id,
        )

    def _make_stage4_deps(sessionmaker, obs):
        return Stage4AnalysisDependencies(
            sessionmaker=sessionmaker,
            claim_analysis_service=ClaimAnalysisService(
                sessionmaker, _E2eClaimModel(observer=obs, provider=provider, model_id=model_id)
            ),
            financial_analysis_service=FinancialAnalysisService(
                sessionmaker,
                _RecordingFixedModel(
                    _financial_decision(),
                    "financial_analysis",
                    observer=obs,
                    provider=provider,
                    model_id=model_id,
                ),
            ),
            macro_analysis_service=MacroAnalysisService(
                sessionmaker,
                _RecordingFixedModel(
                    _macro_decision(),
                    "macro_analysis",
                    observer=obs,
                    provider=provider,
                    model_id=model_id,
                ),
            ),
            valuation_analysis_service=ValuationAnalysisService(
                sessionmaker,
                _RecordingFixedModel(
                    _valuation_decision(),
                    "valuation_analysis",
                    observer=obs,
                    provider=provider,
                    model_id=model_id,
                ),
            ),
            synthesis_service=SynthesisService(sessionmaker),
            synthesis_analysis_service=SynthesisAnalysisService(
                sessionmaker,
                _E2eSynthesisModel(observer=obs, provider=provider, model_id=model_id),
            ),
        )

    def _make_stage5_deps(sessionmaker, obs):
        draft_service = DraftSectionService(
            sessionmaker, _E2eDraftModel(observer=obs, provider=provider, model_id=model_id)
        )
        report_service = ReportService(sessionmaker, draft_service)
        check_service = ReportCheckService(sessionmaker, report_service)
        audit_service = ReportAuditService(sessionmaker, _make_audit(obs), check_service)
        review_service = ReviewActionService(sessionmaker, audit_service)
        revision_service = RevisionService(
            sessionmaker,
            model=_E2eRevisionModel(observer=obs, provider=provider, model_id=model_id),
            draft_section_service=draft_service,
            check_service=check_service,
            review_action_service=review_service,
        )
        report_service._revision_service = revision_service  # noqa: SLF001 — DI 断环
        return Stage5WorkflowDependencies(
            sessionmaker=sessionmaker,
            report_outline_service=ReportOutlineService(sessionmaker),
            draft_section_service=draft_service,
            report_service=report_service,
            report_check_service=check_service,
            report_audit_service=audit_service,
            review_action_service=review_service,
            revision_service=revision_service,
            research_backflow_service=ResearchBackflowService(
                sessionmaker, review_service, report_service
            ),
        )

    return FullModelFactoryBundle(
        provider=provider,
        model_id=model_id,
        create_planner=lambda obs: _E2ePlannerModel(
            plan_payload, observer=obs, provider=provider, model_id=model_id
        ),
        create_evidence=lambda obs: _E2eEvidenceModel(
            observer=obs, provider=provider, model_id=model_id
        ),
        create_claim=lambda obs: _E2eClaimModel(observer=obs, provider=provider, model_id=model_id),
        create_financial=lambda obs: _RecordingFixedModel(
            _financial_decision(),
            "financial_analysis",
            observer=obs,
            provider=provider,
            model_id=model_id,
        ),
        create_macro=lambda obs: _RecordingFixedModel(
            _macro_decision(),
            "macro_analysis",
            observer=obs,
            provider=provider,
            model_id=model_id,
        ),
        create_valuation=lambda obs: _RecordingFixedModel(
            _valuation_decision(),
            "valuation_analysis",
            observer=obs,
            provider=provider,
            model_id=model_id,
        ),
        create_synthesis=lambda obs: _E2eSynthesisModel(
            observer=obs, provider=provider, model_id=model_id
        ),
        create_draft=lambda obs: _E2eDraftModel(observer=obs, provider=provider, model_id=model_id),
        create_audit=_make_audit,
        create_revision=lambda obs: _E2eRevisionModel(
            observer=obs, provider=provider, model_id=model_id
        ),
        create_stage4_deps=_make_stage4_deps,
        create_stage5_deps=_make_stage5_deps,
    )


class _SequencedFactories:
    """按调用次序返回不同 decision（最后重复）。"""

    def __init__(self, factories) -> None:
        self._factories = list(factories)
        self.calls = 0

    def __call__(self, pack):
        idx = min(self.calls, len(self._factories) - 1)
        self.calls += 1
        return self._factories[idx](pack)


# ---------------------------------------------------------------- attempt runner


async def _run_full_attempt(
    monkeypatch,
    tmp_path,
    *,
    label: str,
    attempt_no: int,
    plan_payload: ResearchPlanPayload,
    config: EvalExecutionConfig,
    audit_factories,
    env,
    include_annual_doc: bool = True,
    verify=None,
) -> dict:
    """在全新隔离 PG 上执行一次完整 Full attempt，返回结果 + collection 名。

    `verify(sessionmaker) -> dict`：在隔离 DB 销毁**之前**执行（测试的 post-hoc
    DB 验证必须在这里做，否则 DB 已被 DROP）。
    """
    bundle_root = tmp_path / f"bundle_full_{label}"
    await _build_full_bundle(env, bundle_root, monkeypatch, include_annual_doc=include_annual_doc)

    async with _isolated_target(monkeypatch, tmp_path, label=label) as (
        sessionmaker,
        raw_store,
        temp_url,
    ):
        loader = EvaluationBundleLoader(bundle_root)
        execution_case = loader.load_execution_case("full-case", 1)
        execution_spec = EvalExecutionSpec(
            case_fingerprint=execution_case.case_fingerprint,
            source_snapshot_fingerprint=compute_source_snapshot_fingerprint(
                execution_case.snapshot
            ),
            execution_config_fingerprint=compute_execution_config_fingerprint(config),
            variant_id=EvalVariantId.INSIGHTFORGE_FULL,
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
        runner = create_insightforge_full_runner(
            config=config,
            bundle_loader=loader,
            sessionmaker=sessionmaker,
            raw_store=raw_store,
            chroma=chroma,
            embedding_provider=FakeEmbeddingProvider(),
            model_factory_bundle=_fake_bundle(config, plan_payload, audit_factories),
            checkpoint_uri=to_postgres_connection_uri(temp_url),
        )

        collection_name = f"eval_insightforge_full_{attempt.execution_id.hex}"
        client = await chroma.get_client()
        try:
            result = await execute_variant_attempt(
                attempt=attempt,
                trial_spec=trial_spec,
                execution_spec=execution_spec,
                execution_case=execution_case,
                runner=runner,
            )
            extra = await verify(sessionmaker) if verify is not None else None
        finally:
            await _drop_collection(client, collection_name)
        return {
            "result": result,
            "execution_case": execution_case,
            "sessionmaker": sessionmaker,
            "verify": extra,
            "execution_spec_fingerprint": compute_execution_spec_fingerprint(execution_spec),
        }


# ---------------------------------------------------------------- decision factories


def _pass_decision_factory():
    from tests.audit.fakes import pass_decision

    return pass_decision


def _rewrite_then_pass_factories():
    from tests.integration.test_report_audit_service import (
        omitted_counterevidence_decision,
    )

    return [omitted_counterevidence_decision, _pass_decision_factory()]


def _research_then_pass_factories():
    from tests.integration.test_report_audit_service import research_decision

    return [research_decision, _pass_decision_factory()]


def _human_review_then_pass_factories():
    from tests.integration.test_report_audit_service import human_review_decision

    return [human_review_decision, _pass_decision_factory()]


# ---------------------------------------------------------------- E2E tests


async def test_full_happy_path_all_inputs_audit_executed(monkeypatch, tmp_path, env) -> None:
    """full pipeline：document + macro + financial + valuation 全部消费；audit 真实
    执行（usage 含 audit）；revision 不需要 → 无 revision_writer usage。"""
    config = _make_config()
    plan_payload = _make_full_plan_payload()
    holder = await _run_full_attempt(
        monkeypatch,
        tmp_path,
        label="happy",
        attempt_no=1,
        plan_payload=plan_payload,
        config=config,
        audit_factories=[_pass_decision_factory()],
        env=env,
    )
    result = holder["result"]
    assert result.status == ExecutionAttemptStatus.SUCCESS, f"error_code={result.error_code}"
    assert result.error_code is None
    output = result.variant_output
    assert output is not None
    assert output.variant_id == EvalVariantId.INSIGHTFORGE_FULL
    assert result.variant_output_fingerprint == compute_variant_output_fingerprint(output)
    assert output.report_artifact_ref is None

    # usage：9 个组件全现（audit 必现），revision_writer 不出现（无需修订）。
    components = {r.component_name for r in result.usage_records}
    assert components == _FULL_COMPONENTS
    assert _REVISION_COMPONENT not in components

    # citation 闭合：source_fingerprint ∈ frozen snapshot 的合法语义身份集合
    # （document content_sha256 / macro snapshot_fingerprint / structured
    # artifact_fingerprint——`valid_source_fingerprints` 同口径）。
    assert output.citations
    snapshot = holder["execution_case"].snapshot
    from app.eval.scoring.deterministic import valid_source_fingerprints

    frozen_shas = valid_source_fingerprints(snapshot)
    for citation in output.citations:
        assert citation.source_fingerprint in frozen_shas
    for claim in output.claims:
        assert set(claim.citation_ids) <= {c.citation_id for c in output.citations}


async def test_full_revision_executed_when_audit_requires(monkeypatch, tmp_path, env) -> None:
    """audit 判定 rewrite → Revision 真实执行（revision_writer usage >=1）→ 完成。"""
    config = _make_config()
    holder = await _run_full_attempt(
        monkeypatch,
        tmp_path,
        label="revision",
        attempt_no=1,
        plan_payload=_make_full_plan_payload(),
        config=config,
        audit_factories=_rewrite_then_pass_factories(),
        env=env,
    )
    result = holder["result"]
    assert result.status == ExecutionAttemptStatus.SUCCESS, f"error_code={result.error_code}"
    components = {r.component_name for r in result.usage_records}
    assert _REVISION_COMPONENT in components
    revision_count = sum(1 for r in result.usage_records if r.component_name == _REVISION_COMPONENT)
    assert revision_count >= 1
    assert "audit" in components


async def test_full_backflow_executed_when_audit_requires(monkeypatch, tmp_path, env) -> None:
    """audit 判定 research → 真实 backflow loop（Stage4 a2 + Stage5 a2）→ 完成。

    sse-news 证据源（news_article + sse provider）：round-1 的 news 路由只分给
    xinhuanet → 不提取；backflow 无 provider 过滤且其基础检索 query 与该文档
    正文同文本（向量距离 0）→ 必然命中 → 新 EvidenceCard → ref-aware claims
    变化 → 新 synthesis → verify_progress 成立。
    """
    config = _make_config()
    holder = await _run_full_attempt(
        monkeypatch,
        tmp_path,
        label="backflow",
        attempt_no=1,
        plan_payload=_make_full_plan_payload(include_annual=False),
        config=config,
        audit_factories=_research_then_pass_factories(),
        env=env,
        include_annual_doc=False,
        verify=_verify_backflow_rows,
    )
    result = holder["result"]
    assert result.status == ExecutionAttemptStatus.SUCCESS, f"error_code={result.error_code}"

    # backflow 真实发生：Stage4 / Stage5 各有 >=2 个 child run + backflow 请求行。
    counts = holder["verify"]
    assert counts["stage4"] >= 2, "backflow 必须触发 Stage4 attempt2"
    assert counts["stage5"] >= 2, "backflow 必须触发 Stage5 attempt2"
    assert counts["backflow_requests"] >= 1
    assert result.variant_output is not None
    assert result.variant_output.citations


async def _verify_backflow_rows(sessionmaker) -> dict:
    async with sessionmaker() as session:
        stage4_count = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM workflow_runs WHERE graph_name = 'stage4_analysis'")
                )
            ).scalar_one()
        )
        stage5_count = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM workflow_runs WHERE graph_name = 'stage5_report'")
                )
            ).scalar_one()
        )
        backflow_rows = int(
            (
                await session.execute(text("SELECT count(*) FROM research_backflow_requests"))
            ).scalar_one()
        )
    return {
        "stage4": stage4_count,
        "stage5": stage5_count,
        "backflow_requests": backflow_rows,
    }


async def test_full_human_review_auto_approve_policy(monkeypatch, tmp_path, env) -> None:
    """audit 判定 human_review → evaluation policy 自动 approve → 完成（不跳过
    Audit；Check=pass 由生产 finalize_on_approve 强制）。"""
    config = _make_config()
    holder = await _run_full_attempt(
        monkeypatch,
        tmp_path,
        label="human",
        attempt_no=1,
        plan_payload=_make_full_plan_payload(),
        config=config,
        audit_factories=_human_review_then_pass_factories(),
        env=env,
        verify=_verify_human_decisions,
    )
    result = holder["result"]
    assert result.status == ExecutionAttemptStatus.SUCCESS, f"error_code={result.error_code}"
    components = {r.component_name for r in result.usage_records}
    assert "audit" in components
    # 人工裁决被持久化为 immutable HumanReviewDecision。
    assert holder["verify"] >= 1
    assert result.variant_output is not None
    assert result.variant_output.final_text


async def _verify_human_decisions(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM human_review_decisions"))
            ).scalar_one()
        )
