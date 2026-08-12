"""ResearchFulfillmentService 全链 E2E（stage 7A.2A spec S）。

真实 PostgreSQL + FakeResearchPlannerModel + FakeRetrieval +
FakeEvidenceExtractionModel（**0 真实 DeepSeek / 0 Retrieval / 0 Chroma /
0 Web**）——concentrated 链：create_plan → route → fulfill（verify Plan +
verify Route + prepare → 只消费 missing_needs → 各 executor → 重跑 prepare）。

覆盖（spec G/H/I/Q/R）：
- 全链：缺 document / financial / macro 证据 → 一次 fulfill 全部 RESOLVED →
  `preparation_after.ready_for_analysis=True` + 有效 stage4_request；
- 幂等（spec Q）：第 2 次 fulfill 0 新增写（evidence_cards /
  financial_calculations / macro Evidence 行数不变）；
- module 级 missing（module:macro）被跳过（不独立执行）；
- 未知 need_kind → UNSUPPORTED（service 防御分发）。
"""

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.research_fulfillment.contracts import (
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    ValuationNeedExecutor,
)
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_planning.preparation import (
    MissingReasonCode,
    MissingResearchNeed,
    ResearchPreparationResult,
    ResearchPreparationService,
)
from app.research_planning.router import ResearchSourceRouter
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import (
    _decision_for_chunk,
    _FakeRetrieval,
    _make_hit,
    _seed_evidence_card,
    _seed_revenue_pair,
    _seed_world_bank_provider,
)
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_macro_evidence_service import _seed_macro_chain
from tests.integration.test_research_planning_service import (
    _cleanup,
    _plan_payload,
    _seed_company,
    _seed_research_task,
)
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

# 一次 fulfill 可全部 resolved 的 payload（缺 document/financial/macro 证据，
# 不含 valuation——valuation 恒 manual_required，不 gate readiness）。
_FULFILLMENT_PAYLOAD = dict(
    document_needs=[
        {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"}
    ],
    financial_needs=[
        {
            "need_code": "revenue_change",
            "purpose": "需要营收绝对变化",
            "calculation_code": "absolute_change_cny",
            "metric_code": "revenue",
        }
    ],
    macro_needs=[
        {
            "need_code": "macro_pop",
            "purpose": "需要宏观人口数据",
            "topic_or_indicator": "Population",
            "geography": "CHN",
        }
    ],
    event_needs=[],
    valuation_needs=[],
    analysis_modules=["business_event", "risk", "financial", "macro"],
    research_focus=["经营质量"],
)


# ---------------------------------------------------------------- env


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
    await _seed_world_bank_provider(sessionmaker)
    company_id = await _seed_company(sessionmaker, "600519")
    peer_company_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    task_id = await _seed_research_task(sessionmaker)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "peer_company_ids": peer_company_ids,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


def _planner(sessionmaker, fake: FakeResearchPlannerModel):
    return __import__(
        "app.research_planning.service", fromlist=["ResearchPlanningService"]
    ).ResearchPlanningService(sessionmaker, fake, CompanyIdentityService(sessionmaker))


async def _build_chain(env, monkeypatch) -> tuple[_FakeRetrieval, object]:
    """seed fulfillment 消费的底层数据：news source + 财务观察 + macro 链。

    返回 (FakeRetrieval, FakeEvidenceExtractionModel decision)。document
    executor 用 FakeRetrieval 返回 news chunk 的 hit（0 真实 Retrieval /
    Chroma）。
    """
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    retrieval = _FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunk)])
    await _seed_evidence_card(env)  # env["evidence_card_id"] = 披露源卡
    await _seed_revenue_pair(env)
    await _seed_macro_chain(env, monkeypatch)
    return retrieval, _decision_for_chunk(chunk)


def _service(
    env: dict,
    fake: FakeResearchPlannerModel,
    *,
    retrieval,
    decision,
    preparation=None,
) -> tuple:
    """构造 plan service / router / preparation / fulfillment service。"""
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = preparation or ResearchPreparationService(
        env["sessionmaker"], plan_service, router
    )
    service = ResearchFulfillmentService(
        env["sessionmaker"],
        plan_service,
        router,
        preparation,
        document_executor=DocumentNeedExecutor(
            env["sessionmaker"],
            retrieval,
            FakeEvidenceExtractionModel(decision=decision),
        ),
        financial_executor=FinancialNeedExecutor(env["sessionmaker"]),
        macro_executor=MacroNeedExecutor(env["sessionmaker"]),
        valuation_executor=ValuationNeedExecutor(),
    )
    return plan_service, router, preparation, service


async def _counts(env: dict) -> tuple[int, int, int]:
    from sqlalchemy import text

    async with env["sessionmaker"]() as session:
        cards = int(
            (await session.execute(text("SELECT count(*) FROM evidence_cards"))).scalar_one()
        )
        macros = int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM evidence_cards "
                        "WHERE origin_type = 'macro_observation'"
                    )
                )
            ).scalar_one()
        )
        calcs = int(
            (
                await session.execute(text("SELECT count(*) FROM financial_calculations"))
            ).scalar_one()
        )
    return (cards, macros, calcs)


# ---------------------------------------------------------------- 全链 resolved


async def test_e2e_fulfill_resolves_all_missing_needs(env, monkeypatch) -> None:
    """缺 document/financial/macro 证据 → 一次 fulfill 全 resolved → ready。"""
    retrieval, decision = await _build_chain(env, monkeypatch)
    fake = FakeResearchPlannerModel(_plan_payload(**_FULFILLMENT_PAYLOAD))
    plan_service, router, preparation, service = _service(
        env, fake, retrieval=retrieval, decision=decision
    )
    plan_result = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(plan_result.research_plan_id)

    before = await preparation.prepare_research(plan_result.research_plan_id)
    assert before.ready_for_analysis is False
    missing = {n.need_code for n in before.missing_needs}
    assert {"news_docs", "revenue_change", "macro_pop"} <= missing

    result = await service.fulfill_research_needs(plan_result.research_plan_id)

    by_code = {a.need_code: a for a in result.attempts}
    assert by_code["news_docs"].status == FulfillmentStatus.RESOLVED
    assert by_code["news_docs"].created_artifact_ids
    assert by_code["revenue_change"].status == FulfillmentStatus.RESOLVED
    assert by_code["revenue_change"].created_artifact_ids
    assert by_code["macro_pop"].status == FulfillmentStatus.RESOLVED
    assert by_code["macro_pop"].created_artifact_ids
    assert result.preparation_after.ready_for_analysis is True
    assert result.ready_for_analysis is True
    assert result.stage4_request is not None
    assert result.preparation_before.missing_need_codes
    # 只消费 missing_needs：attempt 都是被执行过的 need（无 module）。
    assert all(a.need_type != "module" for a in result.attempts)


async def test_e2e_second_fulfill_zero_new_writes(env, monkeypatch) -> None:
    """spec Q：全链幂等——第 2 次 fulfill 0 新增写（attempts 为空）。"""
    retrieval, decision = await _build_chain(env, monkeypatch)
    fake = FakeResearchPlannerModel(_plan_payload(**_FULFILLMENT_PAYLOAD))
    plan_service, router, _, service = _service(env, fake, retrieval=retrieval, decision=decision)
    plan_result = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(plan_result.research_plan_id)

    first = await service.fulfill_research_needs(plan_result.research_plan_id)
    assert first.ready_for_analysis is True
    counts_after_first = await _counts(env)

    second = await service.fulfill_research_needs(plan_result.research_plan_id)
    assert second.attempts == []  # 已全 resolved → 无可执行 missing need
    assert second.ready_for_analysis is True
    assert await _counts(env) == counts_after_first  # 0 新增写


# ---------------------------------------------------------------- module 跳过 / 未知 need


async def test_e2e_module_need_is_skipped(env) -> None:
    """module 级 missing（module:macro）不独立执行（补足底层后自动重评估）。"""
    fake = FakeResearchPlannerModel(_plan_payload(macro_needs=[]))
    plan_service, router, _, service = _service(
        env,
        fake,
        retrieval=_FakeRetrieval(),
        decision=None,
    )
    plan_result = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(plan_result.research_plan_id)

    result = await service.fulfill_research_needs(plan_result.research_plan_id)

    # module:macro 出现在 missing 快照里，但不在 attempts（被跳过）。
    assert "module:macro" in result.preparation_before.missing_need_codes
    assert all(a.need_type != "module" for a in result.attempts)
    # 非 module need 正常执行（无 seed → SOURCE_NOT_FOUND / MANUAL_REQUIRED 等）。
    executed = {"news_docs", "events", "revenue_change", "pe_valuation"}
    assert any(a.need_code in executed for a in result.attempts)


async def test_e2e_unknown_need_kind_unsupported(env) -> None:
    """未知 need_kind → service 分发失败 → UNSUPPORTED（防御）。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan_result = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(plan_result.research_plan_id)

    injected = MissingResearchNeed(
        need_code="unknown_need",
        need_kind="unknown_kind",
        reason_code=MissingReasonCode.UNSUPPORTED_NEED,
        detail="注入的未知 need",
    )
    prep_result = ResearchPreparationResult(
        research_plan_id=plan_result.research_plan_id,
        resolved=(),
        module_inputs=(),
        missing_needs=(injected,),
        ready_for_analysis=False,
        stage4_request=None,
    )

    class _StubPreparation:
        async def prepare_research(self, research_plan_id):
            del research_plan_id
            return prep_result

    service = ResearchFulfillmentService(
        env["sessionmaker"],
        plan_service,
        router,
        _StubPreparation(),
        document_executor=DocumentNeedExecutor(
            env["sessionmaker"], _FakeRetrieval(), FakeEvidenceExtractionModel()
        ),
        financial_executor=FinancialNeedExecutor(env["sessionmaker"]),
        macro_executor=MacroNeedExecutor(env["sessionmaker"]),
        valuation_executor=ValuationNeedExecutor(),
    )
    result = await service.fulfill_research_needs(plan_result.research_plan_id)

    assert len(result.attempts) == 1
    attempt = result.attempts[0]
    assert attempt.need_code == "unknown_need"
    assert attempt.need_type == "unknown_kind"
    assert attempt.status == FulfillmentStatus.UNSUPPORTED
    assert attempt.error_code == FulfillmentErrorCode.UNSUPPORTED_NEED
