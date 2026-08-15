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
import json
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
from app.db.models.source_provider import SourceProviderModel
from app.db.session import DatabaseManager
from app.domain.sources import SourceCapability
from app.financial.calculations.contracts import CalculationCode, InputRole
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.source_provider_repository import SourceProviderRepository
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
    SourceRoutePlan,
    SourceRouteType,
    compute_route_fingerprint,
)
from app.research_planning.service import (
    ResearchPlanningService,
    compute_plan_fingerprint,
)
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_financial_claim_service import _calc
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
            {
                "need_code": "revenue_change",
                "purpose": "需要营收绝对变化",
                "calculation_code": "absolute_change_cny",
                "metric_code": "revenue",
            }
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
    """seed 一个带研究问题的 ResearchTask（create_plan 要求恰好 1 个问题）。

    V1.1 closure：modules 覆盖全部 6 个用户模块 + include_relative_valuation=True，
    使 fake planner 的完整 payload（business_event/risk/financial/macro/valuation）
    不被 `apply_selected_modules` 过滤（模块范围强制不改变测试语义）。
    """
    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=end_date,
                modules=[
                    "company_profile",
                    "business",
                    "financial",
                    "events",
                    "macro",
                    "risk",
                ],
                questions=questions if questions is not None else [_QUESTION],
                include_relative_valuation=True,
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


async def test_v2_task_question_change_not_tamper(env) -> None:
    """spec A：v2 冻结 creation-time input → task question 后期变化不是 tamper。

    旧 v1 verify 会重读 task questions 重算 input fingerprint → 误判；v2 只重放
    stored snapshot，question 已冻结，verify 仍通过。
    """
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
    # question 冻结在 snapshot → verify 通过（0 次额外 LLM）。
    await service.verify_research_plan_integrity(result.research_plan_id)
    assert len(fake.calls) == 1


# ================================================================ Gate A：creation-time snapshot


async def test_v2_create_persists_input_snapshot(env) -> None:
    """spec A1/A3：v2 create 持久化 planner_input_payload，verify 通过。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    assert result.replayed is False
    assert result.plan_schema_version == 2
    async with env["sessionmaker"]() as session:
        row = (
            await session.execute(
                text(
                    "SELECT plan_schema_version, planner_input_schema_version, "
                    "       planner_input_payload "
                    "FROM research_plans WHERE research_plan_id = :pid"
                ).bindparams(pid=result.research_plan_id)
            )
        ).one()
    assert row[0] == 2
    assert row[1] == 1
    snapshot = row[2]
    assert isinstance(snapshot, dict)
    assert snapshot["task_id"] == str(env["task_id"])
    assert snapshot["company_id"] == str(env["company_id"])
    assert snapshot["security_code"] == "600519"
    assert snapshot["research_question"] == _QUESTION
    assert snapshot["analysis_as_of"] == "2026-08-10"
    assert snapshot["aliases"] == []  # _seed_company 无 alias 行
    assert snapshot["short_name"] == "600519"
    # verify 通过（0 次额外 LLM）。
    await service.verify_research_plan_integrity(result.research_plan_id)
    assert len(fake.calls) == 1


async def test_v2_add_company_alias_verify_still_passes(env) -> None:
    """spec A：新增 CompanyAlias（master-data 演化）→ v2 verify 仍通过。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "INSERT INTO company_aliases "
                "(alias_id, company_id, alias, normalized_alias, alias_type, "
                " source_provider_key, source_url) "
                "VALUES (CAST(:aid AS uuid), CAST(:cid AS uuid), :alias, :alias, "
                " 'short_name', 'sse', 'https://www.sse.com.cn')"
            ).bindparams(aid=uuid4(), cid=env["company_id"], alias="茅台国酒")
        )
        await session.commit()
    # 不再重读当前 aliases → 新 alias 不改变 input fingerprint。
    await service.verify_research_plan_integrity(result.research_plan_id)
    assert len(fake.calls) == 1


async def test_v2_tampered_input_snapshot_fails_integrity(env) -> None:
    """spec A：修改 stored planner_input_payload → input fingerprint mismatch。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_plans SET planner_input_payload = "
                "jsonb_set(planner_input_payload, '{research_question}', "
                "'\"被篡改的问题\"'::jsonb) WHERE research_plan_id = :pid"
            ).bindparams(pid=result.research_plan_id)
        )
        await session.commit()
    with pytest.raises(ResearchPlanIntegrityError):
        await service.verify_research_plan_integrity(result.research_plan_id)


async def test_v2_tampered_company_id_fails_integrity(env) -> None:
    """spec A：改 row company_id → snapshot identity / task-company 交叉核对失败。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    peer = env["peer_company_ids"][0]
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_plans SET company_id = CAST(:cid AS uuid) "
                "WHERE research_plan_id = :pid"
            ).bindparams(cid=peer, pid=result.research_plan_id)
        )
        await session.commit()
    with pytest.raises(ResearchPlanIntegrityError):
        await service.verify_research_plan_integrity(result.research_plan_id)


async def test_v2_tampered_task_id_fails_integrity(env, sessionmaker) -> None:
    """spec A：改 row task_id → snapshot identity 交叉核对失败。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    result = await service.create_plan(env["task_id"])
    other_task_id = await _seed_research_task(sessionmaker, questions=["另一个问题"])
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE research_plans SET task_id = CAST(:tid AS uuid) "
                "WHERE research_plan_id = :pid"
            ).bindparams(tid=other_task_id, pid=result.research_plan_id)
        )
        await session.commit()
    with pytest.raises(ResearchPlanIntegrityError):
        await service.verify_research_plan_integrity(result.research_plan_id)


async def _seed_v1_legacy_plan(env) -> UUID:
    """直接插入一条满足 v1 约束的 legacy plan 行（无 input snapshot）。"""
    plan_payload = {
        "research_scope": ["business"],
        "document_needs": [
            {"need_code": "news_docs", "purpose": "需要新闻", "source_type": "news_article"}
        ],
        "financial_needs": [],
        "macro_needs": [],
        "event_needs": [],
        "valuation_needs": [],
        "analysis_modules": ["business_event"],
        "research_focus": ["经营质量"],
    }
    input_fp = "a" * 64
    plan_fp = compute_plan_fingerprint(planner_input_fingerprint=input_fp, payload=plan_payload)
    plan_id = uuid4()
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "INSERT INTO research_plans "
                "(research_plan_id, task_id, company_id, plan_schema_version, "
                " planner_name, planner_version, model_id, "
                " planner_input_fingerprint, plan_payload, plan_fingerprint) "
                "VALUES (CAST(:pid AS uuid), CAST(:tid AS uuid), "
                " CAST(:cid AS uuid), 1, 'research_planner', 1, 'test:fake-model', "
                " :input_fp, CAST(:payload AS jsonb), :plan_fp)"
            ).bindparams(
                pid=plan_id,
                tid=env["task_id"],
                cid=env["company_id"],
                input_fp=input_fp,
                payload=json.dumps(plan_payload, ensure_ascii=False),
                plan_fp=plan_fp,
            )
        )
        await session.commit()
    return plan_id


async def test_v1_legacy_fixture_verify_passes(env) -> None:
    """spec A5：v1 legacy 行（无 snapshot）仍可 verify（不重读当前 alias）。"""
    fake = FakeResearchPlannerModel(_plan_payload())
    service = _planner(env["sessionmaker"], fake)
    plan_id = await _seed_v1_legacy_plan(env)
    plan = await service.verify_research_plan_integrity(plan_id)
    assert plan.plan_schema_version == 1
    assert plan.planner_input_payload is None
    # v1 legacy 不触发 LLM。
    assert len(fake.calls) == 0


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
    assert list(entries) == ["news_docs", "revenue_change", "macro_gdp", "events", "pe_valuation"]
    assert entries["news_docs"]["route_type"] == SourceRouteType.NEWS_ARTICLE.value
    assert entries["news_docs"]["provider_keys"]  # xinhuanet 等 enabled provider 快照
    assert entries["revenue_change"]["route_type"] == SourceRouteType.COMPANY_ANNOUNCEMENT.value
    assert entries["macro_gdp"]["route_type"] == SourceRouteType.MACRO_DATA.value
    assert entries["events"]["route_type"] == SourceRouteType.NEWS_ARTICLE.value
    assert entries["pe_valuation"]["route_type"] == SourceRouteType.COMPANY_ANNOUNCEMENT.value
    assert entries["pe_valuation"]["provider_keys"]


async def test_authority_tier_does_not_leak_into_route(env) -> None:
    """spec 7B.1.4 G/H authority_tier 语义审计（Case B 证明）。

    authority_tier 只影响 `SourceProviderRepository.list_providers` 的行序；router
    `_build_entries` 立即用 `sorted({provider_key})` 折叠 providers，丢弃 ORDER BY
    authority_tier → 不进入 `provider_keys` / `route_fingerprint` / 后续 provider
    selection。因此 authority_tier **不是**语义字段，继续不冻结（不进入
    FrozenSourceProviderRef）。

    用真实 `list_providers` + 真实 `_build_entries`（**不 mock 排序代码**），先证明
    测试对 authority_tier 敏感（repo 行序确实随 tier 反转），再证明 router 的可观测
    输出（provider_keys + route fingerprint）在 authority_tier 变化下完全不变。
    """
    sessionmaker = env["sessionmaker"]
    payload = _plan_payload()
    fake = FakeResearchPlannerModel(payload)
    router = _router(sessionmaker, fake)

    def _provider(key: str, tier: int) -> SourceProviderModel:
        return SourceProviderModel(
            provider_key=key,
            display_name=key,
            provider_type="general_web",
            authority_tier=tier,
            homepage_url=f"https://audit.invalid/{key}",
            allowed_domains=[],
            capabilities=["news_article"],
            acquisition_methods=["public_html"],
            exchange_scope=[],
            requires_api_key=False,
            critical_claim_eligible=False,
            enabled=True,
        )

    async with sessionmaker() as session:
        session.add(_provider("audit_tier_a", 1))
        session.add(_provider("audit_tier_b", 4))
        await session.commit()

    async def _repo_order() -> list[str]:
        async with sessionmaker() as session:
            rows = await SourceProviderRepository(session).list_providers(
                authority_tier=None,
                capability=SourceCapability.NEWS_ARTICLE,
                acquisition_method=None,
                exchange=None,
                enabled_only=True,
            )
        return [r.provider_key for r in rows]

    async def _observable_output() -> tuple[list[str], str]:
        entries = await router._build_entries(payload)
        news = next(e for e in entries if e.need_code == "news_docs")
        fp = compute_route_fingerprint(
            plan_fingerprint="f" * 64,
            router_name=ROUTER_NAME,
            router_version=ROUTER_VERSION,
            payload=SourceRoutePlan(entries=entries).normalized_payload(),
        )
        return news.provider_keys, fp

    try:
        # (1) 测试敏感：tier A=1 / B=4 → 行序 [A, B]；router 输出为字母序。
        order = await _repo_order()
        assert order.index("audit_tier_a") < order.index("audit_tier_b")
        keys_first, fp_first = await _observable_output()
        assert "audit_tier_a" in keys_first and "audit_tier_b" in keys_first
        assert keys_first.index("audit_tier_a") < keys_first.index("audit_tier_b")

        # (2) 交换 authority_tier（同 key/capability/enabled）→ 行序反转。
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE source_providers SET authority_tier = "
                    "CASE provider_key WHEN 'audit_tier_a' THEN 4 ELSE 1 END "
                    "WHERE provider_key IN ('audit_tier_a', 'audit_tier_b')"
                )
            )
            await session.commit()
        order = await _repo_order()
        assert order.index("audit_tier_b") < order.index("audit_tier_a")

        # (3) router 可观测输出完全不变（provider_keys + route fingerprint）。
        keys_second, fp_second = await _observable_output()
        assert keys_second == keys_first
        assert fp_second == fp_first
    finally:
        async with sessionmaker() as session:
            await session.execute(
                text("DELETE FROM source_providers WHERE provider_key LIKE 'audit_tier_%'")
            )
            await session.commit()


async def test_route_snapshot_survives_registry_change(env) -> None:
    """spec D：route 是创建时的 registry 快照；禁用/新增 provider 后旧 route verify 仍 PASS。

    verify 只重放 stored route payload + plan fingerprint（不重新 route / 不查
    registry），因此 provider 快照不因 registry 演化而失效。
    """
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    plan = await plan_service.create_plan(env["task_id"])
    routed = await router.route_research_plan(plan.research_plan_id)
    news_keys = [
        e["provider_keys"] for e in routed.route_payload["entries"] if e["need_code"] == "news_docs"
    ][0]
    assert "xinhuanet" in news_keys

    # registry 演化：禁用 xinhuanet + 新增一个 news_article provider。
    try:
        async with env["sessionmaker"]() as session:
            await session.execute(
                text("UPDATE source_providers SET enabled = false WHERE provider_key = 'xinhuanet'")
            )
            await session.execute(
                text(
                    "INSERT INTO source_providers (provider_key, display_name, provider_type, "
                    "authority_tier, homepage_url, allowed_domains, capabilities, "
                    "acquisition_methods) VALUES (:key, :name, 'professional_media', 3, :url, "
                    "CAST(:domains AS jsonb), CAST(:caps AS jsonb), CAST(:methods AS jsonb))"
                ).bindparams(
                    key="test_new_provider",
                    name="测试新增 Provider",
                    url="https://example.com",
                    domains='["example.com"]',
                    caps='["news_article"]',
                    methods='["public_html"]',
                )
            )
            await session.commit()

        # 旧 route verify 仍 PASS，且 stored provider 快照不变。
        route = await router.verify_research_plan_route_integrity(plan.research_plan_id)
        assert route.route_fingerprint == routed.route_fingerprint
        stored_keys = [
            e["provider_keys"]
            for e in route.route_payload["entries"]
            if e["need_code"] == "news_docs"
        ][0]
        assert stored_keys == news_keys  # 快照仍含 xinhuanet（注册表变化不影响）

        # 重新 route 也 replay 同一行（同 plan + router_version 已存在）。
        rerouted = await router.route_research_plan(plan.research_plan_id)
        assert rerouted.replayed is True
    finally:
        # 恢复 registry，避免污染后续测试（共享 DB）。
        async with env["sessionmaker"]() as session:
            await session.execute(
                text("UPDATE source_providers SET enabled = true WHERE provider_key = 'xinhuanet'")
            )
            await session.execute(
                text("DELETE FROM source_providers WHERE provider_key = 'test_new_provider'")
            )
            await session.commit()


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


async def test_route_issuer_ir_provider_available(env) -> None:
    """ISSUER_IR → issuer_official provider（V1.1 closure：registry 已登记）。"""
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
    assert entry["provider_keys"] == ["issuer_official"]


# ================================================================ Preparation


async def test_prepare_ready_true_valid_stage4_request(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
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
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
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
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    # 库中只有 revenue 观察（+ absolute_change calc）；gross_margin 需要
    # operating_cost 观察 → MISSING_METRIC，不误 ready。
    fake = FakeResearchPlannerModel(
        _plan_payload(
            financial_needs=[
                {
                    "need_code": "gross_margin",
                    "purpose": "需要毛利率",
                    "calculation_code": "gross_margin",
                }
            ]
        )
    )
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["gross_margin"] == MissingReasonCode.MISSING_METRIC


async def test_prepare_revenue_yoy_does_not_satisfy_gross_margin_need(env, monkeypatch) -> None:
    """spec B：库里有 revenue yoy calc，但 plan 要 gross_margin → 缺 financial，不误 ready。"""
    ids = await _seed_worker_inputs(env, monkeypatch)
    # 用既有 absolute_change calc 的同一对 observation 追加一条 revenue yoy 派生
    # calculation（观察指纹确定性 → 不能重复 insert 相同观察，直接复用）。
    async with env["sessionmaker"]() as session:
        rows = await session.execute(
            text(
                "SELECT input_role, metric_observation_id FROM financial_calculation_inputs "
                "WHERE calculation_id = :cid"
            ).bindparams(cid=ids["calc"])
        )
        obs_by_role = {InputRole(row.input_role): row.metric_observation_id for row in rows}
    await _calc(env, obs_by_role, code=CalculationCode.YOY_GROWTH_RATE)
    fake = FakeResearchPlannerModel(
        _plan_payload(
            financial_needs=[
                {
                    "need_code": "gross_margin",
                    "purpose": "需要毛利率",
                    "calculation_code": "gross_margin",
                }
            ]
        )
    )
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["gross_margin"] == MissingReasonCode.MISSING_METRIC
    # gross_margin 未解析 → financial module 也不该拿到 calc。
    assert not any(n.need_code == "gross_margin" for n in result.resolved)


async def test_prepare_missing_macro_ready_false(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    fake = FakeResearchPlannerModel(_plan_payload(macro_needs=[]))
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    # macro module 声明了但无 macro need → module:macro 输入为空（0 fake readiness）。
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["module:macro"] == MissingReasonCode.INSUFFICIENT_EVIDENCE


async def test_prepare_missing_valuation_ready_false(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
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
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
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
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    other_card = await _seed_other_company_doc_card(env)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is True
    resolved_ids = {card_id for need in result.resolved for card_id in need.artifact_ids}
    assert other_card not in resolved_ids


async def test_prepare_critical_ineligible_not_boosted(env, monkeypatch) -> None:
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, env["task_id"])
    result = await preparation.prepare_research(plan_result.research_plan_id)
    # 文档卡 critical_claim_eligible=False（seed 默认）→ 投影 0，不因模块需要而提升。
    news_need = next(n for n in result.resolved if n.need_code == "news_docs")
    assert news_need.critical_claim_eligible_count == 0
    assert news_need.min_authority_tier == 3  # _seed_html_source 默认 authority_tier=3
    assert result.ready_for_analysis is True  # critical 元数据不 gate readiness


async def test_prepare_issuer_ir_source_not_found_not_ready(env, monkeypatch) -> None:
    """issuer_ir 有 provider（issuer_official）但无来源 → NOT_FOUND（V1.1 closure）。"""
    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
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
    assert missing["ir_material"] == MissingReasonCode.NOT_FOUND


async def test_prepare_requires_route_before_resolution(env, monkeypatch) -> None:
    """preparation 必须等 route 持久化；未 route → ResearchPlanRouteNotFound。"""
    from app.research_planning.errors import ResearchPlanRouteNotFound

    await _seed_worker_inputs(env, monkeypatch, research_question=_QUESTION)
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)
    plan_result = await plan_service.create_plan(env["task_id"])
    with pytest.raises(ResearchPlanRouteNotFound):
        await preparation.prepare_research(plan_result.research_plan_id)


async def test_prepare_document_evidence_question_mismatch_not_ready(env, monkeypatch) -> None:
    """spec C：同一公司同一 source，卡片提取问题 != 任务研究问题 → 不满足 → ready=false。

    卡片用问题 A（worker seed 默认），任务研究问题用 B（不同问题）。source 存在、
    时间可得，但 EvidenceCard 的 research_question_sha256 不匹配 → 证据不计入。
    """
    await _seed_worker_inputs(env, monkeypatch)  # 卡片 research_question = A
    task_b = await _seed_research_task(
        env["sessionmaker"],
        questions=["评估公司治理结构与股东回报水平。"],
    )
    fake = FakeResearchPlannerModel(_plan_payload())
    plan_result, preparation = await _create_and_route(env["sessionmaker"], fake, task_b)
    result = await preparation.prepare_research(plan_result.research_plan_id)
    assert result.ready_for_analysis is False
    missing = {n.need_code: n.reason_code for n in result.missing_needs}
    assert missing["news_docs"] == MissingReasonCode.INSUFFICIENT_EVIDENCE
    detail = next(n.detail for n in result.missing_needs if n.need_code == "news_docs")
    assert "研究问题" in detail
    # 问题不匹配的证据不能进入 document 证据池 → business/risk module 无输入。
    assert missing["module:business"] == MissingReasonCode.INSUFFICIENT_EVIDENCE
    assert missing["module:risk"] == MissingReasonCode.INSUFFICIENT_EVIDENCE


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
