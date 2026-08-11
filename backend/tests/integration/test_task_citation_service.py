"""Task-scoped citation navigation tests (Stage 6B.2 spec R/S).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer）+ Fake LLM models，全程
**零真实 DeepSeek**。覆盖：

1. **Document Evidence citation（R-1）**：quote / context / source / locator 与
   真实链一致（quote 精确切片自 chunk.text[quote_start:quote_end]）；
2. **Macro Evidence citation（R-2）**：Observation → Snapshot → Series 全链完整；
3. **跨 task scope（R-3/R-4）**：Evidence / Claim 属于别的 task → `CitationNotFound`
   （404），随机 UUID 同样 404（不泄漏「这个 UUID 在别的 task 存在」）；
4. **同一 Evidence 多关系不丢（R-5）**：一条卡被多个 canonical claim 以不同
   relation（supports / context）引用时全部保留；
5. **Document provenance tamper（R-6）**：篡改 document_chunks.text → 409
   `TaskArtifactIntegrityError`（quote 切片契约破坏，不 repair）；
6. **Macro provenance tamper（R-7）**：删除 snapshot → raw artifact 归档链接 →
   409（不 repair）；
7. **0 LLM / 0 network（R-8）**：移除 DEEPSEEK_API_KEY 后 citation 全部成功；
8. **HTTP E2E（S）**：seed completed Stage5 task → GET report 第一段
   evidence_card_id → GET evidence citation 验证 claim relation 与 report
   paragraph 引用一致 → Macro Evidence → citation → macro provenance。
"""

from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select, text

from app.api.dependencies import (
    get_research_execution_service,
    get_task_artifact_service,
    get_task_citation_service,
)
from app.core.config import get_settings
from app.core.errors import CitationNotFound, TaskArtifactIntegrityError
from app.core.runtime import configure_asyncio_runtime
from app.db.models.claim_evidence_link import ClaimEvidenceLinkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.main import create_app
from app.schemas.citation import DocumentProvenance, MacroProvenance
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_service import ResearchExecutionService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_artifact_service import TaskArtifactService
from app.services.task_citation_service import TaskCitationService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.runner import Stage4WorkflowRunner
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_report_audit_service import human_review_decision
from tests.integration.test_research_execution_recovery import _run_stage4_to_completed
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import (
    _AS_OF,
    _QUESTION,
    _good_models,
    _seed_claim_doc_card,
    _seed_research_task,
    _seed_worker_inputs,
)
from tests.integration.test_stage4_workflow import (
    _build_deps as _stage4_deps,
)
from tests.integration.test_stage4_workflow import (
    _request as _stage4_request,
)
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_task_artifact_workspace import (
    _make_artifact_service,
    _run_stage4_graph,
)
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- fixtures


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
async def connection_uri() -> str:
    return to_postgres_connection_uri(get_settings().database_url)


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_revisions(sessionmaker)
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
    await _cleanup_with_revisions(sessionmaker)


# ---------------------------------------------------------------- helpers


def _make_citation_service(
    sessionmaker, manager: LangGraphCheckpointManager
) -> TaskCitationService:
    return TaskCitationService(sessionmaker, _make_artifact_service(sessionmaker, manager))


async def _document_and_macro_card(
    artifact: TaskArtifactService, task_id: UUID
) -> tuple[EvidenceCardModel, EvidenceCardModel]:
    """取一条 document + 一条 macro evidence 卡（真实 ORM 行，含 provenance FK）。"""
    evidence = await artifact.get_evidence(task_id, limit=100, offset=0)
    doc_ids = {e.evidence_card_id for e in evidence.items if e.origin_type == "document_chunk"}
    macro_ids = {e.evidence_card_id for e in evidence.items if e.origin_type == "macro_observation"}
    assert doc_ids and macro_ids, "标准链应同时含 document 与 macro evidence 卡"
    sessionmaker = artifact._sessionmaker
    async with sessionmaker() as session:
        doc_card = (
            (
                await session.execute(
                    select(EvidenceCardModel).where(
                        EvidenceCardModel.evidence_card_id.in_(sorted(doc_ids, key=str))
                    )
                )
            )
            .scalars()
            .first()
        )
        macro_card = (
            (
                await session.execute(
                    select(EvidenceCardModel).where(
                        EvidenceCardModel.evidence_card_id.in_(sorted(macro_ids, key=str))
                    )
                )
            )
            .scalars()
            .first()
        )
    return doc_card, macro_card


# ---------------------------------------------------------------- R-1：document citation


async def test_document_evidence_citation(env, monkeypatch, connection_uri) -> None:
    """R-1：Document citation——quote / context / source / locator 与真实链一致
    （quote 精确切片自 chunk.text[quote_start:quote_end]）。"""
    sessionmaker = env["sessionmaker"]
    manager, _, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        citation = _make_citation_service(sessionmaker, manager)
        task_id = env["task_id"]
        doc_card, _ = await _document_and_macro_card(artifact, task_id)

        resp = await citation.get_evidence_citation(task_id, doc_card.evidence_card_id)
        assert resp.evidence.evidence_card_id == doc_card.evidence_card_id
        assert resp.evidence.statement == doc_card.evidence_statement
        assert resp.evidence.quote_text == doc_card.quote_text
        assert resp.evidence.evidence_type == doc_card.evidence_type
        assert resp.evidence.origin_type == "document_chunk"

        prov = resp.provenance
        assert isinstance(prov, DocumentProvenance)
        assert prov.origin_type == "document_chunk"
        assert prov.source_id == doc_card.source_id
        assert prov.parsed_source_id == doc_card.parsed_source_id
        assert prov.chunk_id == doc_card.chunk_id
        assert prov.provider_key and prov.provider_label
        assert prov.title and prov.source_url
        assert prov.raw_artifact_id and prov.media_type
        assert prov.locator is not None and prov.locator.locator_type
        assert prov.locator_refs
        # context_text：安全纯文本上下文，≤5000 chars，必须含 quote。
        assert len(prov.context_text) <= 5000
        assert doc_card.quote_text in prov.context_text
        # quote 精确切片契约：chunk.text[quote_start:quote_end] == quote_text。
        async with sessionmaker() as session:
            chunk_text = (
                await session.execute(
                    text("SELECT text FROM document_chunks WHERE chunk_id = :cid").bindparams(
                        cid=prov.chunk_id
                    )
                )
            ).scalar_one()
        assert chunk_text[doc_card.quote_start : doc_card.quote_end] == doc_card.quote_text
    finally:
        await manager.close()


# ---------------------------------------------------------------- R-2：macro citation


async def test_macro_evidence_citation(env, monkeypatch, connection_uri) -> None:
    """R-2：Macro citation——Observation → Snapshot → Series 全链完整。"""
    sessionmaker = env["sessionmaker"]
    manager, _, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        citation = _make_citation_service(sessionmaker, manager)
        task_id = env["task_id"]
        _, macro_card = await _document_and_macro_card(artifact, task_id)

        resp = await citation.get_evidence_citation(task_id, macro_card.evidence_card_id)
        assert resp.evidence.origin_type == "macro_observation"
        assert resp.evidence.quote_text is None  # macro 无 quote

        prov = resp.provenance
        assert isinstance(prov, MacroProvenance)
        assert prov.origin_type == "macro_observation"
        assert prov.observation_id == macro_card.macro_observation_id
        assert prov.snapshot_id == macro_card.macro_snapshot_id
        assert prov.series_id == macro_card.macro_series_id
        assert prov.period and prov.indicator and prov.geography
        assert prov.provider_key and prov.provider_label
        assert prov.authority_tier is not None
        assert prov.raw_artifact_id and prov.media_type
        assert prov.artifact_links
        # Observation → Snapshot 身份一致（真实行）。
        async with sessionmaker() as session:
            obs_snapshot_id = (
                await session.execute(
                    text(
                        "SELECT snapshot_id FROM macro_observations WHERE observation_id = :oid"
                    ).bindparams(oid=prov.observation_id)
                )
            ).scalar_one()
        assert prov.snapshot_id == obs_snapshot_id
    finally:
        await manager.close()


# ---------------------------------------------------------------- R-3/R-4：cross-task 404


async def test_evidence_and_claim_cross_task_404(env, monkeypatch, connection_uri) -> None:
    """R-3/R-4：Evidence / Claim 属于别的 task → 404；随机 UUID 同样 404（不泄漏
    存在性）。

    task2 复用 task1 的共享输入（公司级合法）并**只新增一张唯一 URL 卡**，避免
    重种子固定 URL 文档撞 `uq_source_records_provider_url_artifact`（镜像
    test_task_artifact_workspace::test_same_company_task_isolation）。
    """
    sessionmaker = env["sessionmaker"]
    task1_id = env["task_id"]
    ids = await _seed_worker_inputs(env, monkeypatch)
    request_a = _stage4_request(env, ids)
    await _run_stage4_graph(env, connection_uri, request_a, _good_models())

    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        citation = _make_citation_service(sessionmaker, manager)

        # 合法正例：本 task 的 evidence / claim 可引用。
        evidence1 = await artifact.get_evidence(task1_id, limit=100, offset=0)
        assert evidence1.items
        legal_card = evidence1.items[0].evidence_card_id
        legal_claim = (await artifact.get_analysis(task1_id)).claims[0].claim_id
        assert (
            await citation.get_evidence_citation(task1_id, legal_card)
        ).evidence.evidence_card_id == legal_card
        assert (await citation.get_claim_citation(task1_id, legal_claim)).claim_id == legal_claim

        # task2：独立 Stage4，biz 卡换成任务二独有的卡 → 自己的 evidence / claim。
        task2_id = await _seed_research_task(sessionmaker)
        env_b = {**env, "task_id": task2_id}
        extra_b = await _seed_claim_doc_card(
            env,
            statement="另一任务独有的经营现金流转好主张。",
            source_url="https://www.sse.com.cn/2026/0809/taskb-cash.htm",
        )
        items = []
        for item in request_a.analysis_work_items:
            if item.item_id == "biz":
                items.append(
                    item.model_copy(update={"evidence_card_ids": [extra_b["evidence_card_id"]]})
                )
            else:
                items.append(item)
        request_b = Stage4WorkflowRequest(
            task_id=task2_id,
            company_id=env["company_id"],
            research_question=_QUESTION,
            analysis_as_of=_AS_OF,
            analysis_work_items=items,
        )
        await _run_stage4_graph(env_b, connection_uri, request_b, _good_models())

        other_card = extra_b["evidence_card_id"]
        analysis2 = await artifact.get_analysis(task2_id)
        other_claim = next(
            c.claim_id for c in analysis2.claims if other_card in c.evidence_card_ids
        )
        assert other_claim
        # 卡真实存在于库中（属于别的 task），仍必须 404。
        async with sessionmaker() as session:
            assert (
                await session.execute(
                    text(
                        "SELECT count(*) FROM evidence_cards WHERE evidence_card_id = :cid"
                    ).bindparams(cid=other_card)
                )
            ).scalar_one() == 1

        with pytest.raises(CitationNotFound):
            await citation.get_evidence_citation(task1_id, other_card)
        with pytest.raises(CitationNotFound):
            await citation.get_claim_citation(task1_id, other_claim)
        # 随机 UUID：同样的 404，不暴露存在性。
        with pytest.raises(CitationNotFound):
            await citation.get_evidence_citation(task1_id, uuid4())
        with pytest.raises(CitationNotFound):
            await citation.get_claim_citation(task1_id, uuid4())
    finally:
        await manager.close()


# ---------------------------------------------------------------- R-5：multi-relation preserved


async def test_same_evidence_multi_relation_preserved(env, monkeypatch, connection_uri) -> None:
    """R-5：同一 Evidence 被多个 canonical claim 以不同 relation（supports +
    context）引用时全部保留（不压成单一 relation、不丢）。"""
    sessionmaker = env["sessionmaker"]
    manager, _, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        citation = _make_citation_service(sessionmaker, manager)
        task_id = env["task_id"]
        claim_scope = await artifact.resolve_claim_scope(task_id)
        assert claim_scope

        # 独立推导 canonical claim 对每张卡的关系集。
        async with sessionmaker() as session:
            rows = await session.execute(
                select(
                    ClaimEvidenceLinkModel.evidence_card_id,
                    ClaimEvidenceLinkModel.relation,
                ).where(ClaimEvidenceLinkModel.claim_id.in_(sorted(claim_scope, key=str)))
            )
            relations_by_card: dict[UUID, set[str]] = {}
            for card_id, relation in rows.all():
                relations_by_card.setdefault(card_id, set()).add(relation)
        multi = {cid: rels for cid, rels in relations_by_card.items() if len(rels) >= 2}
        # 标准链 biz 卡：business claim(supports) + financial claim(context)。
        assert multi, "标准链应存在被多个 canonical claim 以不同 relation 引用的 evidence 卡"
        card_id, expected_relations = next(iter(multi.items()))
        assert {"supports", "context"} <= expected_relations

        resp = await citation.get_evidence_citation(task_id, card_id)
        got = {(r.claim_id, r.relation) for r in resp.claim_relations}
        async with sessionmaker() as session:
            expected = set(
                (claim_id, relation)
                for claim_id, relation in (
                    await session.execute(
                        select(
                            ClaimEvidenceLinkModel.claim_id,
                            ClaimEvidenceLinkModel.relation,
                        ).where(
                            ClaimEvidenceLinkModel.evidence_card_id == card_id,
                            ClaimEvidenceLinkModel.claim_id.in_(sorted(claim_scope, key=str)),
                        )
                    )
                ).all()
            )
        assert got == expected, "citation 的 claim_relations 必须与 DB 独立推导一致（不丢）"
        assert len({r.claim_id for r in resp.claim_relations}) >= 2
        assert {r.relation for r in resp.claim_relations} >= {"supports", "context"}
        # claim_statement 都来自真实 claims 行。
        assert all(r.claim_statement for r in resp.claim_relations)
    finally:
        await manager.close()


# ---------------------------------------------------------------- R-6/R-7：provenance tamper


async def test_document_provenance_tamper_integrity(env, monkeypatch, connection_uri) -> None:
    """R-6：篡改 document_chunks.text → quote 切片契约破坏 → 409
    TaskArtifactIntegrityError（spec M，不 repair）。"""
    sessionmaker = env["sessionmaker"]
    manager, _, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        citation = _make_citation_service(sessionmaker, manager)
        task_id = env["task_id"]
        doc_card, _ = await _document_and_macro_card(artifact, task_id)
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE document_chunks SET text = 'TAMPERED ' || text WHERE chunk_id = :cid"
                ).bindparams(cid=doc_card.chunk_id)
            )
            await session.commit()
        with pytest.raises(TaskArtifactIntegrityError):
            await citation.get_evidence_citation(task_id, doc_card.evidence_card_id)
    finally:
        await manager.close()


async def test_macro_provenance_tamper_integrity(env, monkeypatch, connection_uri) -> None:
    """R-7：删除 snapshot → raw artifact 归档链接（raw-artifact link 缺失）→ 409
    TaskArtifactIntegrityError（spec M，不 repair）。"""
    sessionmaker = env["sessionmaker"]
    manager, _, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    try:
        artifact = _make_artifact_service(sessionmaker, manager)
        citation = _make_citation_service(sessionmaker, manager)
        task_id = env["task_id"]
        _, macro_card = await _document_and_macro_card(artifact, task_id)
        async with sessionmaker() as session:
            await session.execute(
                text("DELETE FROM macro_snapshot_artifacts WHERE snapshot_id = :sid").bindparams(
                    sid=macro_card.macro_snapshot_id
                )
            )
            await session.commit()
        with pytest.raises(TaskArtifactIntegrityError):
            await citation.get_evidence_citation(task_id, macro_card.evidence_card_id)
    finally:
        await manager.close()


# ---------------------------------------------------------------- R-8：0 LLM / 0 network


async def test_citation_no_llm_no_network(env, monkeypatch, connection_uri) -> None:
    """R-8：移除 DEEPSEEK_API_KEY 后生产 DI 构建 citation service，全部卡可引用
    （0 LLM / 0 model construction / 0 network）。"""
    sessionmaker = env["sessionmaker"]
    manager, _, _ = await _run_stage4_to_completed(env, monkeypatch, connection_uri)
    try:
        monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
        get_settings.cache_clear()
        deps = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        artifact = TaskArtifactService.from_dependencies(sessionmaker, manager, deps)
        citation = TaskCitationService(sessionmaker, artifact)
        task_id = env["task_id"]
        evidence = await artifact.get_evidence(task_id, limit=100, offset=0)
        assert evidence.items
        for card in evidence.items:
            resp = await citation.get_evidence_citation(task_id, card.evidence_card_id)
            assert resp.evidence.evidence_card_id == card.evidence_card_id
            assert resp.provenance.origin_type in ("document_chunk", "macro_observation")
    finally:
        await manager.close()


# ---------------------------------------------------------------- HTTP E2E（spec S）


@pytest_asyncio.fixture
async def app_ctx(tmp_path, sessionmaker, connection_uri) -> dict:
    """真实 FastAPI + 真实 Service + Fake models；override 3 个 DI（execution +
    artifact + citation 共用同一 checkpoint/manager）。"""
    await _cleanup_with_revisions(sessionmaker)
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await SourceRegistryService(sessionmaker).seed_defaults()

    checkpoint = LangGraphCheckpointManager(connection_uri)
    await checkpoint.setup()
    execution = ResearchExecutionService(
        sessionmaker=sessionmaker,
        checkpoint_manager=checkpoint,
        company_identity=CompanyIdentityService(sessionmaker),
        stage4_runner_factory=lambda: Stage4WorkflowRunner(
            sessionmaker, checkpoint, _stage4_deps(sessionmaker, _good_models())
        ),
        stage5_runner_factory=lambda: Stage5WorkflowRunner(
            sessionmaker,
            checkpoint,
            _stage5_deps(
                sessionmaker,
                draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
                audit_model=FakeAuditModel(decision_factory=human_review_decision),
                revision_model=FakeRevisionWriterModel(),
            ),
        ),
    )
    artifact_service = _make_artifact_service(sessionmaker, checkpoint)
    citation_service = TaskCitationService(sessionmaker, artifact_service)

    app = create_app(get_settings())
    app.dependency_overrides[get_research_execution_service] = lambda: execution
    app.dependency_overrides[get_task_artifact_service] = lambda: artifact_service
    app.dependency_overrides[get_task_citation_service] = lambda: citation_service
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "sessionmaker": sessionmaker,
                "checkpoint": checkpoint,
                "execution": execution,
                "raw_store": raw_store,
                "artifact": artifact_service,
                "citation": citation_service,
            }
            await execution.close()
    await checkpoint.close()
    await _cleanup_with_revisions(sessionmaker)


async def test_e2e_citation_via_http(app_ctx, monkeypatch) -> None:
    """spec S：seed completed Stage5 task → HTTP GET report 第一段 evidence_card_id
    → GET evidence citation 验证 claim relation 与 report paragraph 引用一致 →
    Macro Evidence → citation → macro provenance。0 real DeepSeek。"""
    from tests.integration.test_stage6_vertical_slice import (
        _create_task,
        _execute_payload,
        _full_work_items,
        _seed_full_worker_env,
        _wait_for_workspace,
    )

    client = app_ctx["client"]

    # ---- seed completed Stage5 task ----
    task = await _create_task(app_ctx)
    task_id = UUID(task["task_id"])

    env, ids = await _seed_full_worker_env(app_ctx, task_id, monkeypatch)
    response = await client.post(
        f"/api/v1/tasks/{task_id}/execute",
        json=_execute_payload(_full_work_items(env, ids)),
    )
    assert response.status_code == 202, response.text

    workspace = await _wait_for_workspace(
        client,
        task_id,
        lambda b: bool(b["current_run"] and b["current_run"]["status"] == "waiting_human"),
    )
    stage5_run_id = workspace["current_run"]["run_id"]
    response = await client.post(
        f"/api/v1/workflow-runs/{stage5_run_id}/actions",
        json={"action_type": "approve", "comment": "审核通过"},
    )
    assert response.status_code == 202, response.text
    workspace = await _wait_for_workspace(
        client,
        task_id,
        lambda b: bool(b["current_run"] and b["current_run"]["status"] == "completed"),
    )
    assert workspace["artifact_summary"]["report_count"] >= 1

    # ---- GET report → 第一段 evidence_card_id ----
    response = await client.get(f"/api/v1/tasks/{task_id}/report")
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["report_id"] and report["sections"]
    first_section = report["sections"][0]
    first_paragraph = first_section["paragraphs"][0]
    paragraph_claim_ids = [UUID(raw) for raw in first_paragraph["claim_ids"]]
    paragraph_evidence_ids = [UUID(raw) for raw in first_paragraph["evidence_card_ids"]]
    assert paragraph_claim_ids and paragraph_evidence_ids

    # ---- GET evidence citation：claim relation 与 report paragraph 引用一致 ----
    evidence_card_id = paragraph_evidence_ids[0]
    response = await client.get(f"/api/v1/tasks/{task_id}/citations/evidence/{evidence_card_id}")
    assert response.status_code == 200, response.text
    citation = response.json()
    assert citation["evidence"]["evidence_card_id"] == str(evidence_card_id)
    assert citation["evidence"]["quote_text"]
    related_claim_ids = {UUID(row["claim_id"]) for row in citation["claim_relations"]}
    assert related_claim_ids, "citation 应有 claim relations"
    assert set(paragraph_claim_ids) <= related_claim_ids, (
        "report paragraph 引用的 claim 必须出现在 evidence citation 的 claim relations 中"
    )
    assert {row["relation"] for row in citation["claim_relations"]} <= {
        "supports",
        "contradicts",
        "context",
    }
    prov = citation["provenance"]
    if prov["origin_type"] == "document_chunk":
        assert prov["source_id"] and prov["chunk_id"] and prov["locator"]
        assert len(prov["context_text"]) <= 5000
    else:
        assert prov["observation_id"] and prov["series_id"]

    # ---- Macro Evidence → citation → macro provenance ----
    response = await client.get(f"/api/v1/tasks/{task_id}/evidence")
    assert response.status_code == 200, response.text
    macro_ids = [
        item["evidence_card_id"]
        for item in response.json()["items"]
        if item["origin_type"] == "macro_observation"
    ]
    assert macro_ids, "任务应含 macro evidence"
    response = await client.get(f"/api/v1/tasks/{task_id}/citations/evidence/{macro_ids[0]}")
    assert response.status_code == 200, response.text
    macro_prov = response.json()["provenance"]
    assert macro_prov["origin_type"] == "macro_observation"
    assert macro_prov["observation_id"]
    assert macro_prov["snapshot_id"] and macro_prov["series_id"]
    assert macro_prov["indicator"] and macro_prov["geography"]
    assert macro_prov["provider_key"] and macro_prov["provider_label"]
    assert macro_prov["raw_artifact_id"] and macro_prov["media_type"]
    assert macro_prov["artifact_links"]
