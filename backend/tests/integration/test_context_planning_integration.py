"""Research Context Intelligence integration tests (P0/P1/P2).

真实 PostgreSQL：context_needs 进入 plan → router 映射 → preparation 解析
（missing 不阻塞 ready）→ ContextNeedExecutor 统一发现/检索（Fake 注入）。
"""

from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.research_fulfillment.contracts import FulfillmentStatus
from app.research_fulfillment.executors import ContextNeedExecutor
from app.research_planning.contracts import (
    ContextNeed,
    ContextNeedType,
    DocumentNeed,
    ResearchDocumentNeedType,
    ResearchPlanPayload,
)
from app.research_planning.preparation import (
    MissingReasonCode,
    ResearchPreparationService,
)
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_discovery.contracts import (
    SourceDiscoveryOutcome,
    SourceDiscoveryRequest,
)
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import (
    _decision_for_chunk,
    _FakeRetrieval,
    _make_context,
    _make_entry,
    _make_hit,
    _make_need,
)
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_research_planning_service import _cleanup, _seed_research_task
from tests.integration.test_valuation_claim_service import _seed_company
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "分析宁德时代的经营质量、主要风险和估值水平。"


class FakeDiscovery:
    """替身 SourceDiscoveryService：固定 outcome + 记录 requests。"""

    def __init__(self, outcome: SourceDiscoveryOutcome | None = None) -> None:
        self._outcome = outcome or SourceDiscoveryOutcome(acquired=False, exhausted=True)
        self.calls: list[SourceDiscoveryRequest] = []

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryOutcome:
        self.calls.append(request)
        return self._outcome


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
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    task_id = await _seed_research_task(
        sessionmaker, questions=[_QUESTION], end_date=date(2026, 8, 10)
    )
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


def _payload_with_context(**overrides) -> ResearchPlanPayload:
    base = dict(
        research_scope=["business", "financial"],
        document_needs=[
            DocumentNeed(
                need_code="annual_2024",
                purpose="需要年度报告",
                source_type=ResearchDocumentNeedType.ANNUAL_REPORT,
                period="2024",
            )
        ],
        financial_needs=[
            {
                "need_code": "revenue_yoy_2024",
                "purpose": "需要营收同比",
                "calculation_code": "yoy_growth_rate",
                "metric_code": "revenue",
                "period": "2024",
            }
        ],
        context_needs=[
            ContextNeed(
                need_code="lithium_price",
                purpose="需要锂价走势",
                context_type=ContextNeedType.COMMODITY_MARKET,
                topic="锂价",
            ),
            ContextNeed(
                need_code="ev_install",
                purpose="需要动力电池装机量",
                context_type=ContextNeedType.INDUSTRY_METRIC,
                topic="动力电池装机量",
            ),
        ],
        analysis_modules=["business_event", "financial"],
        research_focus=["经营质量"],
    )
    base.update(overrides)
    return ResearchPlanPayload.model_validate(base)


async def test_context_missing_does_not_block_ready(env) -> None:
    """核心 need 满足 + context missing → ready_for_analysis 仍为 True。"""
    fake = FakeResearchPlannerModel(_payload_with_context())
    plan_service = ResearchPlanningService(
        env["sessionmaker"], fake, CompanyIdentityService(env["sessionmaker"])
    )
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)

    plan = await plan_service.create_plan(env["task_id"])
    route = await router.route_research_plan(plan.research_plan_id)
    # context route entries 存在。
    payload = route.route_payload
    context_entries = [e for e in payload["entries"] if e["need_kind"] == "context"]
    assert len(context_entries) == 2
    assert {e["need_code"] for e in context_entries} == {"lithium_price", "ev_install"}

    result = await preparation.prepare_research(plan.research_plan_id)

    # context missing 不阻塞 ready（无任何核心证据时整体仍 not ready——用
    # 带核心证据的 payload 验证 context 不影响）。
    context_missing = [m for m in result.missing_needs if m.need_kind == "context"]
    assert len(context_missing) == 2
    assert all(
        m.reason_code in (MissingReasonCode.NOT_FOUND, MissingReasonCode.INSUFFICIENT_EVIDENCE)
        for m in context_missing
    )


async def test_context_resolved_joins_evidence_pool(env) -> None:
    """context 文档证据落库后 → resolved 且进入 business 证据池。"""
    # seed 行业新闻 source（topic 相关检索由 query 保证；解析按来源类型）。
    src, parsed_id, _, chunks = await _seed_html_source(
        env,
        provider_key="xinhuanet",
        document_type="news_article",
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    # 为 context 创建一张证据卡（research_question 匹配）。
    decision = _decision_for_chunk(chunks[0])
    extractor = FakeEvidenceExtractionModel(decision=decision)
    retrieval = _FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunks[0])])
    from app.research_fulfillment.executors import DocumentNeedExecutor

    doc_executor = DocumentNeedExecutor(
        env["sessionmaker"],
        retrieval_service=retrieval,
        extractor_model=extractor,
    )
    # 用 ContextNeedExecutor 的文档路径触发（需要 context payload）。
    discovery = FakeDiscovery()
    from app.research_fulfillment.executors.macro import MacroNeedExecutor

    context_executor = ContextNeedExecutor(
        env["sessionmaker"],
        doc_executor,
        MacroNeedExecutor(env["sessionmaker"]),
        discovery=discovery,
    )
    payload = _payload_with_context()
    attempt = await context_executor.fulfill(
        context=_make_context(env, payload=payload),
        need=_make_need("ev_install", need_kind="context"),
        entry=_make_entry("ev_install", need_kind="context", provider_keys=("xinhuanet",)),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert len(attempt.created_artifact_ids) == 1
    # 已有 eligible source（seed 的 topic 匹配来源）→ 复用，不触发 discovery。
    assert discovery.calls == []
    # 证据卡已落库（document_chunk origin）。
    async with env["sessionmaker"]() as session:
        n = int(
            (
                await session.execute(
                    text("SELECT count(*) FROM evidence_cards WHERE origin_type='document_chunk'")
                )
            ).scalar_one()
        )
    assert n == 1
    # topic-aware 检索：query 必须包含 context topic。
    assert any("动力电池装机量" in q.query_text for q in retrieval.calls)


async def test_context_executor_macro_timeseries_unresolved_when_no_data(env) -> None:
    """macro_timeseries context：无观测 + discovery exhausted → unresolved（不阻塞）。"""
    from app.research_fulfillment.executors import DocumentNeedExecutor, MacroNeedExecutor

    discovery = FakeDiscovery()
    context_executor = ContextNeedExecutor(
        env["sessionmaker"],
        DocumentNeedExecutor(
            env["sessionmaker"],
            retrieval_service=_FakeRetrieval(),
            extractor_model=FakeEvidenceExtractionModel(),
        ),
        MacroNeedExecutor(env["sessionmaker"]),
        discovery=discovery,
    )
    payload = _payload_with_context(
        context_needs=[
            ContextNeed(
                need_code="gdp_cn",
                purpose="需要中国GDP",
                context_type=ContextNeedType.MACRO_TIMESERIES,
                topic="GDP",
                geography="中国",
            )
        ]
    )
    attempt = await context_executor.fulfill(
        context=_make_context(env, payload=payload),
        need=_make_need("gdp_cn", need_kind="context"),
        entry=_make_entry("gdp_cn", need_kind="context", provider_keys=("world_bank",)),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert len(discovery.calls) == 1
    assert discovery.calls[0].need_kind == "macro"


async def test_context_document_discovery_when_no_sources(env) -> None:
    """无现有来源 → 统一发现被触发（news_article + other），exhausted → UNRESOLVED。"""
    from app.research_fulfillment.executors import DocumentNeedExecutor, MacroNeedExecutor

    discovery = FakeDiscovery()
    context_executor = ContextNeedExecutor(
        env["sessionmaker"],
        DocumentNeedExecutor(
            env["sessionmaker"],
            retrieval_service=_FakeRetrieval(),
            extractor_model=FakeEvidenceExtractionModel(),
        ),
        MacroNeedExecutor(env["sessionmaker"]),
        discovery=discovery,
    )
    payload = _payload_with_context()
    attempt = await context_executor.fulfill(
        context=_make_context(env, payload=payload),
        need=_make_need("lithium_price", need_kind="context"),
        entry=_make_entry("lithium_price", need_kind="context", provider_keys=("xinhuanet",)),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    # discovery 请求：news_article + other 两类文档来源，topic 透传。
    assert {c.source_type for c in discovery.calls} == {"news_article", "other"}
    assert all(c.need_kind == "document" for c in discovery.calls)
    assert all(c.topic == "锂价" for c in discovery.calls)
