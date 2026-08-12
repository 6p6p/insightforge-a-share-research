"""Research planning service / router / preparation integration tests (stage 7A.1 spec T).

真实 PostgreSQL + FakeResearchPlannerModel（全程 **0 真实 DeepSeek**）：

- **ResearchPlanningService**：create（持久化 immutable plan）、replay（同 input →
  同一行，**0 次额外 LLM 调用**）、并发最终 1 行、tamper（plan_payload / task
  question）→ `ResearchPlanIntegrityError`、malformed 输出传播、单问题规则、
  task 不存在；
- **ResearchSourceRouter**：deterministic route（0 LLM）、同 (plan, router_version)
  replay 同一行、route tamper → `ResearchPlanRouteIntegrityError`、route_type 映射
  + provider 快照、ISSUER_IR → provider_unavailable；
- **ResearchPreparationService**：ready=true → 有效 `Stage4WorkflowRequest`；
  missing document / financial / macro / valuation → ready=false；future / wrong
  company evidence 排除；critical-ineligible 不提升；module 无输入 → 0 fake
  readiness；provider 不可用 → provider_unavailable。
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import (
    MissingResearchQuestion,
    ResearchExecutionRequiresSingleQuestion,
    TaskNotFound,
)
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_planning.contracts import (
    ResearchPlanPayload,
)
from app.research_planning.errors import (
    ResearchPlanIntegrityError,
    ResearchPlannerMalformedOutput,
    ResearchPlanRouteIntegrityError,
)
from app.research_planning.preparation import (
    MissingReasonCode,
    ResearchPreparationService,
)
from app.research_planning.repository import ResearchPlanRepository
from app.research_planning.router import (
    ROUTER_NAME,
    ROUTER_VERSION,
    ResearchSourceRouter,
    SourceRouteType,
)
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_stage4_workflow import _seed_worker_inputs
from tests.integration.test_valuation_claim_service import _seed_company
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "分析贵州茅台的经营质量、主要风险和估值水平。"
_AS_OF = date(2026, 8, 10)


# ---------------------------------------------------------------- plan 构造


def _plan_payload(**overrides) -> ResearchPlanPayload:
    """Fake planner 返回的合法 ResearchPlanPayload（needs 映射到 _seed_worker_inputs）。"""
    base = {
        "research_scope": ["business", "financial", "macro", "valuation"],
        "document_needs": [
            {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"}
        ],
        "financial_needs": [
            {"need_code": "revenue", "purpose": "需要营收数据", "metric_code": "revenue"}
        ],
        "macro_needs": [
            {"need_code": "macro_gdp", "purpose": "需要宏观数据", "topic_or_indicator": "中国GDP"}
        ],
        "event_needs": [{"need_code": "events", "purpose": "需要公司事件", "topic": "公司事件"}],
        "valuation_needs": [{"need_code": "pe_valuation", "metric_code": "pe_ttm"}],
        "analysis_modules": ["business_event", "risk", "financial", "macro", "valuation"],
        "research_focus": ["经营质量", "估值水平"],
    }
    base.update(overrides)
    return ResearchPlanPayload.model_validate(base)


# ---------------------------------------------------------------- env / cleanup


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        # research planning 先于上游（FK RESTRICT）。
        await session.execute(text("DELETE FROM research_plan_routes"))
        await session.execute(text("DELETE FROM research_plans"))
        await session.execute(text("DELETE FROM workflow_events"))
        await session.execute(text("DELETE FROM workflow_runs"))
        await session.execute(text("DELETE FROM research_tasks"))
        await session.execute(text("DELETE FROM draft_sections"))
        await session.execute(text("DELETE FROM report_outlines"))
        await session.execute(text("DELETE FROM claim_synthesis_results"))
        await session.execute(text("DELETE FROM claim_synthesis_input_links"))
        await session.execute(text("DELETE FROM claim_synthesis_runs"))
        await session.execute(text("DELETE FROM claim_relative_valuation_comparison_links"))
        await session.execute(text("DELETE FROM relative_valuation_claim_profiles"))
        await session.execute(text("DELETE FROM claim_financial_calculation_links"))
        await session.execute(text("DELETE FROM financial_calculation_inputs"))
        await session.execute(text("DELETE FROM financial_calculations"))
        await session.execute(text("DELETE FROM financial_metric_observations"))
        await session.execute(text("DELETE FROM macro_transmission_evidence_links"))
        await session.execute(text("DELETE FROM macro_transmission_chains"))
        await session.execute(text("DELETE FROM claim_evidence_links"))
        await session.execute(text("DELETE FROM claims"))
        await session.execute(text("DELETE FROM relative_valuation_comparison_peers"))
        await session.execute(text("DELETE FROM relative_valuation_comparisons"))
        await session.execute(text("DELETE FROM valuation_metric_observations"))
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
        await session.execute(text("DELETE FROM news_source_verifications"))
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


async def _seed_research_task(
    sessionmaker,
    *,
    questions: list[str] | None = None,
    end_date: date = _AS_OF,
) -> UUID:
    """seed 一个带研究问题的 ResearchTask（create_plan 要求恰好 1 个问题）。"""
    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=end_date,
                modules=["company_profile"],
                questions=questions if questions is not None else [_QUESTION],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


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
    peer_company_ids = [await _seed_company(sessionmaker, f"6005{2 + i:02d}") for i in range(3)]
    task_id = await _seed_research_task(sessionmaker)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "target_company_id": company_id,
        "peer_company_ids": peer_company_ids,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


# ---------------------------------------------------------------- helpers


def _planner(sessionmaker, fake: FakeResearchPlannerModel) -> ResearchPlanningService:
    return ResearchPlanningService(sessionmaker, fake, CompanyIdentityService(sessionmaker))


def _router(sessionmaker, fake: FakeResearchPlannerModel) -> ResearchSourceRouter:
    return ResearchSourceRouter(sessionmaker, _planner(sessionmaker, fake))


def _preparation(sessionmaker, fake: FakeResearchPlannerModel) -> ResearchPreparationService:
    plan_service = _planner(sessionmaker, fake)
    return ResearchPreparationService(
        sessionmaker, plan_service, ResearchSourceRouter(sessionmaker, plan_service)
    )


async def _create_and_route(sessionmaker, fake: FakeResearchPlannerModel, task_id: UUID):
    """create plan → route → 返回 (plan_result, router, preparation)。"""
    plan_service = _planner(sessionmaker, fake)
    router = ResearchSourceRouter(sessionmaker, plan_service)
    preparation = ResearchPreparationService(sessionmaker, plan_service, router)
    plan_result = await plan_service.create_plan(task_id)
    await router.route_research_plan(plan_result.research_plan_id)
    return plan_result, preparation


# ================================================================ Planner service


async def test_create_plan_persists_valid_plan(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    assert result.replayed is False
    assert len(result.planner_input_fingerprint) == 64
    assert len(result.plan_fingerprint) == 64
    assert result.model_id == fake.model_id
    assert result.plan_payload["analysis_modules"] == [
        "business_event",
        "risk",
        "financial",
        "macro",
        "valuation",
    ]
    assert len(fake.calls) == 1
    # 持久化后 verify 通过（0 次额外 LLM）。
    await service.verify_research_plan_integrity(result.research_plan_id)
    assert len(fake.calls) == 1


async def test_replay_same_input_returns_same_row(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    first = await service.create_plan(env["task_id"])
    second = await service.create_plan(env["task_id"])
    assert second.replayed is True
    assert second.research_plan_id == first.research_plan_id
    assert second.plan_fingerprint == first.plan_fingerprint
    assert len(fake.calls) == 1  # replay 命中 → 0 次额外 LLM


async def test_concurrent_create_single_row(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    results = await asyncio.gather(*(service.create_plan(env["task_id"]) for _ in range(5)))
    plan_ids = {r.research_plan_id for r in results}
    assert len(plan_ids) == 1
    # 并发无 Python 锁：多个 generate 可能发生，但 ON CONFLICT 保证 DB 最终 1 行。
    async with env["sessionmaker"]() as session:
        assert await ResearchPlanRepository(session).count() == 1


async def test_tampered_payload_fails_integrity(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_plans SET plan_payload = "
                "jsonb_set(plan_payload, '{document_needs,0,purpose}', '\"hacked\"'::jsonb) "
                "WHERE research_plan_id = :pid"
            ).bindparams(pid=result.research_plan_id)
        )
        await session.commit()
    with pytest.raises(ResearchPlanIntegrityError):
        await service.verify_research_plan_integrity(result.research_plan_id)


async def test_tampered_task_question_fails_integrity(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_tasks SET questions = "
                "'[\"被篡改的问题\"]'::jsonb WHERE task_id = :tid"
            ).bindparams(tid=env["task_id"])
        )
        await session.commit()
    with pytest.raises(ResearchPlanIntegrityError):
        await service.verify_research_plan_integrity(result.research_plan_id)


async def test_malformed_output_propagates(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload(), fail_with=ResearchPlannerMalformedOutput())
    service = _planner(env["sessionmaker"], fake)
    with pytest.raises(ResearchPlannerMalformedOutput):
        await service.create_plan(env["task_id"])
    # malformed 不落库。
    async with env["sessionmaker"]() as session:
        assert await ResearchPlanRepository(session).count() == 0


async def test_single_question_rules(env, sessionmaker) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    no_question = await _seed_research_task(sessionmaker, questions=[])
    with pytest.raises(MissingResearchQuestion):
        await service.create_plan(no_question)
    multi = await _seed_research_task(sessionmaker, questions=["Q1", "Q2"])
    with pytest.raises(ResearchExecutionRequiresSingleQuestion):
        await service.create_plan(multi)


async def test_task_not_found(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    with pytest.raises(TaskNotFound):
        await service.create_plan(uuid4())


# ================================================================ Router


async def test_route_deterministic_replay(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan = await plan_service.create_plan(env["task_id"])
    first = await router.route_research_plan(plan.research_plan_id)
    second = await router.route_research_plan(plan.research_plan_id)
    assert second.replayed is True
    assert second.route_plan_id == first.route_plan_id
    assert second.route_fingerprint == first.route_fingerprint
    assert second.router_name == ROUTER_NAME
    assert second.router_version == ROUTER_VERSION


async def test_route_mapping_and_provider_snapshot(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan = await plan_service.create_plan(env["task_id"])
    routed = await router.route_research_plan(plan.research_plan_id)
    entries = {e["need_code"]: e for e in routed.route_payload["entries"]}
    assert list(entries) == ["news_docs", "revenue", "macro_gdp", "events", "pe_valuation"]
    assert entries["news_docs"]["route_type"] == SourceRouteType.NEWS_ARTICLE.value
    assert entries["news_docs"]["provider_keys"]  # xinhuanet 等 enabled provider 快照
    assert entries["revenue"]["route_type"] == SourceRouteType.COMPANY_ANNOUNCEMENT.value
    assert entries["macro_gdp"]["route_type"] == SourceRouteType.MACRO_DATA.value
    assert entries["events"]["route_type"] == SourceRouteType.NEWS_ARTICLE.value
    assert entries["pe_valuation"]["route_type"] == SourceRouteType.COMPANY_ANNOUNCEMENT.value
    assert entries["pe_valuation"]["provider_keys"]


async def test_route_tamper_fails_integrity(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(plan.research_plan_id)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_plan_routes SET route_payload = "
                "jsonb_set(route_payload, '{entries,0,provider_keys,0}', '\"evil\"'::jsonb) "
                "WHERE research_plan_id = :pid"
            ).bindparams(pid=plan.research_plan_id)
        )
        await session.commit()
    with pytest.raises(ResearchPlanRouteIntegrityError):
        await router.verify_research_plan_route_integrity(plan.research_plan_id)


async def test_plan_tamper_breaks_route_verify(env) -> None:
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(plan.research_plan_id)
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_plans SET plan_payload = "
                "jsonb_set(plan_payload, '{research_focus,0}', '\"hacked\"'::jsonb) "
                "WHERE research_plan_id = :pid"
            ).bindparams(pid=plan.research_plan_id)
        )
        await session.commit()
    # route verify 先做 plan verify → plan tamper 被上游拦截。
    with pytest.raises(ResearchPlanIntegrityError):
        await router.verify_research_plan_route_integrity(plan.research_plan_id)


async def test_route_issuer_ir_provider_unavailable(env) -> None:
    """ISSUER_IR 当前 registry 无 provider → provider_keys 为空（不伪造可用性）。"""
    fake = FakeResearchPlannerModel(
        _plan_payload(
            document_needs=[
                {
                    "need_code": "ir_material",
                    "purpose": "需要 IR 材料",
                    "source_type": "issuer_ir_material",
                }
            ]
        )
    )
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan = await plan_service.create_plan(env["task_id"])
    routed = await router.route_research_plan(plan.research_plan_id)
    entry = next(e for e in routed.route_payload["entries"] if e["need_code"] == "ir_material")
    assert entry["route_type"] == SourceRouteType.ISSUER_IR.value
    assert entry["provider_keys"] == []


# ================================================================ Preparation


async def test_prepare_ready_true_valid_stage4_request(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)

    assert result.ready_for_analysis is True
    assert result.missing_needs == ()
    req = result.stage4_request
    assert req is not None
    assert req.task_id == env["task_id"]
    assert req.company_id == env["company_id"]
    assert req.research_question == _QUESTION
    assert req.analysis_as_of == _AS_OF
    items = req.analysis_work_items
    assert len(items) == 5
    item_ids = [item.item_id for item in items]
    assert len(set(item_ids)) == 5  # item_id 唯一
    by_type = {item.analysis_type: item for item in items}
    assert by_type["business"].evidence_card_ids
    assert by_type["risk"].evidence_card_ids
    assert by_type["financial"].calculation_ids
    assert by_type["macro"].macro_driver_evidence_ids
    assert by_type["macro"].company_evidence_ids
    assert by_type["valuation"].comparison_ids
    # resolved 记录每个 need。
    assert len(result.resolved) == 5


async def test_prepare_missing_document_ready_false(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(
        _plan_payload(
            document_needs=[
                {
                    "need_code": "annual_report_2024",
                    "purpose": "需要年报",
                    "source_type": "annual_report",
                    "period": "2024",
                },
                {"need_code": "news_docs", "purpose": "需要新闻", "source_type": "news_article"},
            ]
        )
    )
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    assert result.stage4_request is None
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["annual_report_2024"] == MissingReasonCode.NOT_FOUND


async def test_prepare_missing_financial_ready_false(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(
        _plan_payload(
            financial_needs=[
                {"need_code": "net_profit", "purpose": "需要净利润", "metric_code": "net_profit"}
            ]
        )
    )
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["net_profit"] == MissingReasonCode.MISSING_METRIC


async def test_prepare_missing_macro_ready_false(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(_plan_payload(macro_needs=[]))
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    # macro module 声明了但无 macro need → module:macro 输入为空（0 fake readiness）。
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["module:macro"] == MissingReasonCode.INSUFFICIENT_EVIDENCE


async def test_prepare_missing_valuation_ready_false(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    # 只 seed 了 pe_ttm comparison；plan 要 ps_ttm → MISSING_VALUATION_COMPARISON。
    fake = FakeResearchPlannerModel(
        _plan_payload(valuation_needs=[{"need_code": "ps_valuation", "metric_code": "ps_ttm"}])
    )
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["ps_valuation"] == MissingReasonCode.MISSING_VALUATION_COMPARISON


async def test_prepare_future_evidence_excluded(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    # 加一张 future 新闻卡（published_at 晚于 as_of → no-lookahead 排除）。
    future_card = await _seed_future_doc_card(env)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is True
    resolved_ids = {card_id for need in result.resolved for card_id in need.artifact_ids}
    assert future_card not in resolved_ids
    for module in result.module_inputs:
        assert future_card not in module.artifact_ids


async def test_prepare_wrong_company_evidence_excluded(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    other_card = await _seed_other_company_doc_card(env)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is True
    resolved_ids = {card_id for need in result.resolved for card_id in need.artifact_ids}
    assert other_card not in resolved_ids


async def test_prepare_critical_ineligible_not_boosted(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    # 文档卡 critical_claim_eligible=False（seed 默认）→ 投影 0，不因模块需要而提升。
    news_need = next(n for n in result.resolved if n.need_code == "news_docs")
    assert news_need.critical_claim_eligible_count == 0
    assert news_need.min_authority_tier == 3  # _seed_html_source 默认 authority_tier=3
    assert result.ready_for_analysis is True  # critical 元数据不 gate readiness


async def test_prepare_provider_unavailable_not_ready(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(
        _plan_payload(
            document_needs=[
                {
                    "need_code": "ir_material",
                    "purpose": "需要 IR 材料",
                    "source_type": "issuer_ir_material",
                }
            ]
        )
    )
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["ir_material"] == MissingReasonCode.PROVIDER_UNAVAILABLE


async def test_prepare_requires_route_before_resolution(env, monkeypatch) -> None:
    """preparation 必须等 route 持久化；未 route → ResearchPlanRouteNotFound。"""
    from app.research_planning.errors import ResearchPlanRouteNotFound

    await _seed_worker_inputs(env, monkeypatch)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)
    plan_result = await plan_service.create_plan(env["task_id"])
    with pytest.raises(ResearchPlanRouteNotFound):
        await preparation.prepare_research(plan_result.research_plan_id)


# ---------------------------------------------------------------- seed helpers


async def _seed_future_doc_card(env) -> UUID:
    """一张 future 新闻 document card（published_at 晚于 analysis_as_of）。"""
    from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
    from app.services.evidence_card_service import EvidenceCardService
    from tests.integration.test_evidence_card_service import _seed_html_source

    _, _, _, chunks = await _seed_html_source(
        env,
        document_type="news_article",
        published_at=datetime(2026, 9, 1, tzinfo=UTC),
        source_url="https://www.xinhuanet.com/2026/0901/future.htm",
    )
    chunk = chunks[0]
    result = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="未来某新闻。",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=0,
            quote_end=8,
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    return result.evidence_card_id


async def _seed_other_company_doc_card(env) -> UUID:
    """另一家公司的 document card（company 过滤应排除）。"""
    from app.evidence.contracts import EvidenceCardDraft, EvidenceConfidence, EvidenceType
    from app.repositories.company_repository import CompanyRepository
    from app.services.evidence_card_service import EvidenceCardService
    from tests.integration.test_evidence_card_service import _seed_html_source

    other_company_id = uuid4()
    async with env["sessionmaker"]() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=other_company_id,
                exchange="SZSE",
                security_code="000001",
                identity_key="SZSE:000001",
                board="szse_main",
                official_name="其他公司",
                short_name="其他",
                listing_status="listed",
                identity_source_provider_key="szse",
                identity_source_url="https://www.szse.cn",
            )
        )
        await session.commit()
    other_env = dict(env)
    other_env["company_id"] = other_company_id
    _, _, _, chunks = await _seed_html_source(
        other_env,
        document_type="news_article",
        published_at=datetime(2026, 8, 7, tzinfo=UTC),
        source_url="https://www.xinhuanet.com/2026/0807/other.htm",
    )
    chunk = chunks[0]
    result = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=_QUESTION,
            evidence_statement="其他公司的新闻。",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=0,
            quote_end=8,
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    return result.evidence_card_id
