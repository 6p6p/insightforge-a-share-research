"""Frozen Plan Execution Context integration tests (stage 7A.2A spec B5).

Task 在 Plan 创建后被修改 → 既有 Plan 的执行语义**保持 frozen**：
`get_verified_execution_context` 从 stored `planner_input_payload` 派生
research_question / analysis_as_of / company_id，**不读当前 ResearchTask 字段**。
集中覆盖：

- **P1 frozen**：Task Q1/D1 → create P1 → 改 Task Q2/D2 → P1 verify PASS +
  context 仍 Q1/D1；再 create_plan → P2 snapshot Q2/D2 + fingerprint != P1；
- **Preparation 用 frozen Q1/D1**：Gate C 只接受 hash(Q1) 的证据（Q2 卡被拒），
  stage4_request question=Q1 / as_of=D1；
- **Fulfillment 同源 frozen**：document RetrievalQuery 含 Q1 不含 Q2，extractor
  收到 Q1，新卡 research_question_sha256=hash(Q1)；
- **v1 legacy plan**：verify 历史 PASS，但自动执行（prepare/fulfill）→
  `ResearchPlanLegacyExecutionUnsupported`（不拿当前 Task 猜历史 question/cutoff）。

真实 PG + FakeResearchPlannerModel + FakeRetrieval + FakeEvidenceExtractionModel
（0 真实 DeepSeek / 0 Retrieval / 0 Chroma / 0 Web）。
"""

import json
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
    compute_research_question_sha256,
)
from app.research_fulfillment.contracts import FulfillmentStatus
from app.research_fulfillment.executors import (
    DocumentNeedExecutor,
    FinancialNeedExecutor,
    MacroNeedExecutor,
    ValuationNeedExecutor,
)
from app.research_fulfillment.service import ResearchFulfillmentService
from app.research_planning.errors import ResearchPlanLegacyExecutionUnsupported
from app.research_planning.preparation import MissingReasonCode, ResearchPreparationService
from app.research_planning.router import ResearchSourceRouter
from app.research_planning.service import (
    ResearchPlanningService,
    compute_plan_fingerprint,
)
from app.services.company_identity_service import CompanyIdentityService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import (
    _decision_for_chunk,
    _FakeRetrieval,
    _make_hit,
)
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_migration_0028_downgrade_guard import _hex64
from tests.integration.test_research_planning_service import (
    _cleanup,
    _plan_payload,
    _seed_company,
    _seed_research_task,
)
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_Q1 = "分析贵州茅台的经营质量、主要风险和估值水平。"
_D1 = date(2026, 8, 10)
_Q2 = "评估贵州茅台的市场竞争力与股东回报水平。"
_D2 = date(2026, 8, 12)

# 只含 document need 的 payload：business module 输入 = 文档证据池；无
# financial/macro/event/valuation need → 无其它 missing 干扰 stage4 断言。
_DOC_ONLY = dict(
    document_needs=[
        {"need_code": "news_docs", "purpose": "需要公司新闻", "source_type": "news_article"}
    ],
    financial_needs=[],
    macro_needs=[],
    event_needs=[],
    valuation_needs=[],
    analysis_modules=["business_event"],
    research_scope=["business"],
)


# ---------------------------------------------------------------- env / helpers


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
    task_id = await _seed_research_task(sessionmaker, questions=[_Q1], end_date=_D1)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


def _planner(sessionmaker, fake: FakeResearchPlannerModel) -> ResearchPlanningService:
    return ResearchPlanningService(sessionmaker, fake, CompanyIdentityService(sessionmaker))


async def _mutate_task(sessionmaker, task_id: UUID, *, questions, end_date) -> None:
    """直接把当前 Task 改为 Q2/D2（模拟用户在 Plan 创建后修改研究任务）。"""
    async with sessionmaker() as session:
        await session.execute(
            text(
                "UPDATE research_tasks "
                "SET questions = CAST(:q AS jsonb), research_end_date = :d "
                "WHERE task_id = :tid"
            ).bindparams(q=json.dumps(questions), d=end_date, tid=task_id)
        )
        await session.commit()


async def _create_card(env, chunk, research_question: str) -> UUID:
    """直接登记一张指定 research_question 的 EvidenceCard（fingerprint 含 question）。"""
    result = await EvidenceCardService(env["sessionmaker"]).create_card(
        EvidenceCardDraft(
            research_question=research_question,
            evidence_statement=f"关于 {research_question} 的披露。",
            evidence_type=EvidenceType.METRIC,
            chunk_id=chunk.chunk_id,
            quote_start=0,
            quote_end=20,
            extractor_name="test-extractor",
            extractor_version=1,
            extractor_model_id="test-model",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
    )
    return result.evidence_card_id


async def _seed_v1_plan(sessionmaker, task_id: UUID, company_id: UUID) -> UUID:
    """SQL 插入一条 **self-consistent** 的 v1 legacy plan（无 planner_input_payload）。"""
    plan_id = uuid4()
    input_fp = _hex64()
    payload = {"research_scope": ["business"]}
    plan_fp = compute_plan_fingerprint(planner_input_fingerprint=input_fp, payload=payload)
    async with sessionmaker() as session:
        await session.execute(
            text(
                "INSERT INTO research_plans "
                "(research_plan_id, task_id, company_id, plan_schema_version, "
                " planner_name, planner_version, model_id, "
                " planner_input_fingerprint, plan_payload, plan_fingerprint) "
                "VALUES (CAST(:pid AS uuid), CAST(:tid AS uuid), CAST(:cid AS uuid), "
                " 1, 'research_planner', 1, 'test:fake-model', "
                " :input_fp, CAST(:payload AS jsonb), :plan_fp)"
            ).bindparams(
                pid=plan_id,
                tid=task_id,
                cid=company_id,
                input_fp=input_fp,
                payload=json.dumps(payload),
                plan_fp=plan_fp,
            )
        )
        await session.commit()
    return plan_id


# ---------------------------------------------------------------- P1 frozen


async def test_p1_frozen_after_task_mutation_and_p2_snapshot(env) -> None:
    """P1 冻结 Q1/D1；Task 改 Q2/D2 后 P1 verify PASS + context 仍 Q1/D1；P2 捕获 Q2/D2。"""
    plan_service = _planner(env["sessionmaker"], FakeResearchPlannerModel(_plan_payload()))
    p1 = await plan_service.create_plan(env["task_id"])

    await _mutate_task(env["sessionmaker"], env["task_id"], questions=[_Q2], end_date=_D2)

    # P1 不因 Task 修改而失效（verify 只重放 frozen snapshot / FK identity）。
    verified = await plan_service.verify_research_plan_integrity(p1.research_plan_id)
    assert verified.research_plan_id == p1.research_plan_id

    ctx = await plan_service.get_verified_execution_context(p1.research_plan_id)
    assert ctx.task_id == env["task_id"]
    assert ctx.company_id == env["company_id"]
    assert ctx.research_question == _Q1
    assert ctx.analysis_as_of == _D1

    # 再 create_plan → P2 捕获 Q2/D2，fingerprint != P1（新行）。
    p2 = await plan_service.create_plan(env["task_id"])
    assert p2.replayed is False
    assert p2.research_plan_id != p1.research_plan_id
    assert p2.planner_input_fingerprint != p1.planner_input_fingerprint
    assert p2.plan_fingerprint != p1.plan_fingerprint
    ctx2 = await plan_service.get_verified_execution_context(p2.research_plan_id)
    assert ctx2.research_question == _Q2
    assert ctx2.analysis_as_of == _D2


async def test_preparation_gate_c_accepts_only_frozen_question(env) -> None:
    """Gate C 用 frozen Q1：Q2 卡被拒，Q1 卡被接受；stage4 question=Q1/as_of=D1。"""
    fake = FakeResearchPlannerModel(_plan_payload(**_DOC_ONLY))
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)
    p1 = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(p1.research_plan_id)

    await _mutate_task(env["sessionmaker"], env["task_id"], questions=[_Q2], end_date=_D2)

    _, _, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]

    # 只有 Q2 卡 → Gate C（hash(Q1)）拒绝 → document need missing。
    q2_card = await _create_card(env, chunk, _Q2)
    first = await preparation.prepare_research(p1.research_plan_id)
    news = next(n for n in first.missing_needs if n.need_code == "news_docs")
    assert news.reason_code == MissingReasonCode.INSUFFICIENT_EVIDENCE
    assert first.ready_for_analysis is False

    # 补 Q1 卡 → 接受 → resolved + ready + stage4 question=Q1/as_of=D1。
    q1_card = await _create_card(env, chunk, _Q1)
    assert q1_card != q2_card
    second = await preparation.prepare_research(p1.research_plan_id)
    assert {n.need_code for n in second.missing_needs} <= {"module:business_event"}
    assert second.ready_for_analysis is True
    assert second.stage4_request is not None
    assert second.stage4_request.research_question == _Q1
    assert second.stage4_request.analysis_as_of == _D1


async def test_fulfillment_retrieval_and_extraction_use_frozen_question(env) -> None:
    """document RetrievalQuery 含 Q1 不含 Q2；extractor 收到 Q1；新卡 hash=hash(Q1)。"""
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    retrieval = _FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunk)])
    extractor = FakeEvidenceExtractionModel(decision=_decision_for_chunk(chunk))

    fake = FakeResearchPlannerModel(_plan_payload(**_DOC_ONLY))
    plan_service = _planner(env["sessionmaker"], fake)
    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)
    p1 = await plan_service.create_plan(env["task_id"])
    await router.route_research_plan(p1.research_plan_id)

    await _mutate_task(env["sessionmaker"], env["task_id"], questions=[_Q2], end_date=_D2)

    service = ResearchFulfillmentService(
        env["sessionmaker"],
        plan_service,
        router,
        preparation,
        document_executor=DocumentNeedExecutor(env["sessionmaker"], retrieval, extractor),
        financial_executor=FinancialNeedExecutor(env["sessionmaker"]),
        macro_executor=MacroNeedExecutor(env["sessionmaker"]),
        valuation_executor=ValuationNeedExecutor(),
    )
    result = await service.fulfill_research_needs(p1.research_plan_id)

    by_code = {a.need_code: a for a in result.attempts}
    assert by_code["news_docs"].status == FulfillmentStatus.RESOLVED
    assert retrieval.calls, "必须发生检索"
    query = retrieval.calls[0]
    assert _Q1 in query.query_text
    assert _Q2 not in query.query_text
    # extractor 收到 frozen Q1。
    assert extractor.calls and extractor.calls[0][0] == _Q1
    # 新卡 research_question_sha256 = hash(Q1)。
    assert by_code["news_docs"].created_artifact_ids
    card_id = by_code["news_docs"].created_artifact_ids[0]
    async with env["sessionmaker"]() as session:
        stored_sha = (
            await session.execute(
                text(
                    "SELECT research_question_sha256 FROM evidence_cards "
                    "WHERE evidence_card_id = :cid"
                ).bindparams(cid=card_id)
            )
        ).scalar_one()
    assert stored_sha == compute_research_question_sha256(_Q1)
    assert stored_sha != compute_research_question_sha256(_Q2)
    assert result.ready_for_analysis is True
    assert result.stage4_request["research_question"] == _Q1
    assert result.stage4_request["analysis_as_of"] == _D1.isoformat()


# ---------------------------------------------------------------- v1 legacy


async def test_v1_legacy_plan_verify_ok_but_execution_rejected(env) -> None:
    """v1 plan：verify 历史 PASS；自动执行（context/prepare/fulfill）→ legacy 拒绝。"""
    plan_service = _planner(env["sessionmaker"], FakeResearchPlannerModel(_plan_payload()))
    v1_id = await _seed_v1_plan(env["sessionmaker"], env["task_id"], env["company_id"])

    # 历史完整性可验证（v1 policy：replay stored payload，不读当前 alias）。
    verified = await plan_service.verify_research_plan_integrity(v1_id)
    assert verified.research_plan_id == v1_id

    # 自动执行入口全部拒绝：不拿当前 Task 猜历史 v1 question/cutoff。
    with pytest.raises(ResearchPlanLegacyExecutionUnsupported):
        await plan_service.get_verified_execution_context(v1_id)

    router = ResearchSourceRouter(env["sessionmaker"], plan_service)
    preparation = ResearchPreparationService(env["sessionmaker"], plan_service, router)
    with pytest.raises(ResearchPlanLegacyExecutionUnsupported):
        await preparation.prepare_research(v1_id)

    service = ResearchFulfillmentService(
        env["sessionmaker"],
        plan_service,
        router,
        preparation,
        document_executor=DocumentNeedExecutor(
            env["sessionmaker"], _FakeRetrieval(), FakeEvidenceExtractionModel()
        ),
        financial_executor=FinancialNeedExecutor(env["sessionmaker"]),
        macro_executor=MacroNeedExecutor(env["sessionmaker"]),
        valuation_executor=ValuationNeedExecutor(),
    )
    with pytest.raises(ResearchPlanLegacyExecutionUnsupported):
        await service.fulfill_research_needs(v1_id)
