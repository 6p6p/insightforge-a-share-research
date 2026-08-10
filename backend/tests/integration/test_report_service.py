"""ReportService + ReportCheckService integration tests (stage 5C, spec G/Q/R/S/T).

真实 PostgreSQL + Fake Writer + 真实 LangGraph + PG Checkpointer，全程
**零真实 DeepSeek**（Fake 模型都是确定性返回）。

覆盖（spec T report 清单）：
- E2E：Outline → Fake Writer v2 → 全部 DraftSections → Report → Deterministic
  Check(pass)：report 3 sections 严格按 Outline order、fingerprint 64hex、全部
  10 项 v1 checks pass；verify_report_integrity 完整重建通过；
- replay：同 outline + 同 selected drafts → 同 report_id（replayed=True）；同
  report 再跑 checks → 同 check_result_id（replayed=True）；
- 并发：asyncio.gather 同输入 → 只有 1 个 Report；
- 拒绝路径（0 写）：missing draft section / draft 属于其他 outline →
  ReportAssemblyError；
- draft 变化（不同 writer model 重写）→ 新指纹 → 新 Report；旧 Report 保留且
  仍可完整验证；
- tamper：report_payload / report_fingerprint 篡改 → verify_report_integrity 拒绝
  ReportIntegrityError（不自动 repair）；draft 篡改 → 上游 DraftSectionIntegrityError；
- checks：report 变化（新 report_fingerprint）→ 新 CheckResult（旧行保留）。
"""

import asyncio
import json
from datetime import date
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.draft_section.contracts import DraftSectionRequest
from app.draft_section.errors import DraftSectionIntegrityError
from app.draft_section.service import DraftSectionService
from app.report.check_service import ReportCheckService
from app.report.contracts import (
    CHECK_STATUS_PASS,
    REPORT_CHECK_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    ReportAssemblyDraft,
)
from app.report.errors import ReportAssemblyError, ReportIntegrityError
from app.report.service import ReportService
from app.report_outline.service import ReportOutlineService
from app.repositories.research_task_repository import ResearchTaskRepository
from app.services.source_registry_service import SourceRegistryService
from app.stage4.runner import Stage4WorkflowRunner
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_draft_section_service import (
    _build_deps,
    _create_outline,
    _two_theme_models,
)
from tests.integration.test_stage4_workflow import (
    _cleanup,
    _good_models,
    _request,
    _seed_worker_inputs,
)
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)


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


async def _cleanup_with_reports(sessionmaker) -> None:
    """先删 5C 报告层（FK RESTRICT 引用 report_outlines / companies），再走公共
    _cleanup（否则报告行挡住 report_outlines 删除）。"""
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM report_check_results"))
        await session.execute(text("DELETE FROM reports"))
        await session.commit()
    await _cleanup(sessionmaker)


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker, monkeypatch) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup_with_reports(sessionmaker)
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
    await _cleanup_with_reports(sessionmaker)


async def _seed_research_task(sessionmaker) -> UUID:
    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=date(2026, 12, 31),
                modules=["company_profile"],
                questions=[],
                require_plan_approval=False,
            )
        )
        await session.commit()
    return task_id


# ---------------------------------------------------------------- helpers


def _report_service(env, fake: FakeDraftSectionModel) -> ReportService:
    return ReportService(
        env["sessionmaker"],
        DraftSectionService(env["sessionmaker"], fake),
    )


async def _draft_all_sections(env, outline_id: UUID, fake) -> dict[str, UUID]:
    """起草 outline 的全部 sections（S1/S2/S3），返回 section_id -> draft_section_id。"""
    service = DraftSectionService(env["sessionmaker"], fake)
    ids: dict[str, UUID] = {}
    for section_id in ("S1", "S2", "S3"):
        result = await service.create_or_get_section(
            DraftSectionRequest(outline_id=outline_id, section_id=section_id)
        )
        ids[section_id] = result.draft_section_id
    return ids


async def _report_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int((await session.execute(text("SELECT count(*) FROM reports"))).scalar_one())


async def _check_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM report_check_results"))).scalar_one()
        )


async def _create_two_outlines(env, monkeypatch, connection_uri) -> tuple[UUID, UUID]:
    """同一批 worker inputs 跑两次 Stage4（不同合成输出）→ 两个不同 outline。

    不能直接调用 `_create_outline` 两次：每次都会重 seed 固定 URL 的
    source_records（content_sha256 确定性 artifact_id）→ 唯一约束冲突。这里
    seed 一次 inputs，两次运行 Stage4 graph（claims 按 fingerprint replay，
    合成输出不同 → 不同 result fingerprint → 不同 outline）。
    """
    ids = await _seed_worker_inputs(env, monkeypatch)
    request = _request(env, ids)
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        deps_a = _build_deps(env["sessionmaker"], _two_theme_models())
        runner_a = Stage4WorkflowRunner(env["sessionmaker"], manager, deps_a)
        run_a = await runner_a.create_stage4_run(request)
        result_a = await runner_a.execute_stage4(run_a.run_id, request)

        deps_b = _build_deps(env["sessionmaker"], _good_models())
        runner_b = Stage4WorkflowRunner(env["sessionmaker"], manager, deps_b)
        run_b = await runner_b.create_stage4_run(request)
        result_b = await runner_b.execute_stage4(run_b.run_id, request)
    finally:
        await manager.close()

    outline_service = ReportOutlineService(env["sessionmaker"])
    oa = await outline_service.create_or_get_outline(UUID(result_a["synthesis_result_id"]))
    ob = await outline_service.create_or_get_outline(UUID(result_b["synthesis_result_id"]))
    assert oa.outline_id != ob.outline_id
    return oa.outline_id, ob.outline_id


# ---------------------------------------------------------------- E2E


async def test_report_e2e_create_and_checks_pass(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    draft_ids = await _draft_all_sections(env, outline_id, fake)
    assert len(fake.calls) == 3  # 每 section 恰好一次模型调用

    service = _report_service(env, fake)
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )

    assert report.replayed is False
    assert report.section_count == 3
    assert report.outline_id == outline_id
    assert report.company_id == env["company_id"]
    assert report.report_schema_version == REPORT_SCHEMA_VERSION
    assert len(report.report_fingerprint) == 64
    assert await _report_count(env["sessionmaker"]) == 1

    # public read-side verify：完整重建通过，sections 严格按 Outline order。
    verified = await service.verify_report_integrity(report.report_id)
    sections = verified.report_payload["sections"]
    assert [s["section_id"] for s in sections] == ["S1", "S2", "S3"]
    assert [s["section_order"] for s in sections] == [1, 2, 3]
    assert {s["section_type"] for s in sections} == {"theme", "risks_and_gaps"}
    assert [UUID(s["draft_section_id"]) for s in sections] == [
        draft_ids[s["section_id"]] for s in sections
    ]
    for section in sections:
        assert section["paragraphs"]  # 每个 section 至少 1 段

    # Deterministic Check → pass（verified Report 全部通过）。
    check_service = ReportCheckService(env["sessionmaker"], service)
    check = await check_service.run_report_checks(report.report_id)
    assert check.status == CHECK_STATUS_PASS
    assert check.findings == ()
    assert check.check_schema_version == REPORT_CHECK_SCHEMA_VERSION
    assert len(check.check_fingerprint) == 64
    assert check.replayed is False
    assert await _check_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- replay / concurrency


async def test_report_replay_same_row(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    draft = ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))

    first = await service.create_or_get_report(draft)
    assert first.replayed is False

    second = await service.create_or_get_report(draft)
    assert second.report_id == first.report_id
    assert second.report_fingerprint == first.report_fingerprint
    assert second.replayed is True
    assert await _report_count(env["sessionmaker"]) == 1


async def test_report_concurrent_same_input_single_row(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    draft = ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))

    first, second = await asyncio.gather(
        service.create_or_get_report(draft), service.create_or_get_report(draft)
    )

    assert first.report_id == second.report_id
    assert await _report_count(env["sessionmaker"]) == 1


# ---------------------------------------------------------------- assembly rejection (0 writes)


async def test_report_missing_draft_section_rejected(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    # 只选 S1 + S2，缺 S3 → coverage 拒绝（0 写）。
    with pytest.raises(ReportAssemblyError, match="missing draft section"):
        await service.create_or_get_report(
            ReportAssemblyDraft(
                outline_id=outline_id,
                draft_section_ids=(draft_ids["S1"], draft_ids["S2"]),
            )
        )
    assert await _report_count(env["sessionmaker"]) == 0


async def test_report_wrong_outline_draft_rejected(env, monkeypatch, connection_uri) -> None:
    outline_id, other_outline_id = await _create_two_outlines(env, monkeypatch, connection_uri)
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    # 第二个 outline 的 S1 草稿属于另一个 outline（自身可完整验证）→ 装配拒绝（0 写）。
    other_fake = FakeDraftSectionModel(decision_factory=valid_decision_for)
    other_s1 = await DraftSectionService(env["sessionmaker"], other_fake).create_or_get_section(
        DraftSectionRequest(outline_id=other_outline_id, section_id="S1")
    )

    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    with pytest.raises(ReportAssemblyError, match="different outline"):
        await service.create_or_get_report(
            ReportAssemblyDraft(
                outline_id=outline_id,
                draft_section_ids=(other_s1.draft_section_id, draft_ids["S2"], draft_ids["S3"]),
            )
        )
    assert await _report_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- draft change → new report


async def test_report_draft_change_new_report_old_preserved(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake_a = FakeDraftSectionModel(decision_factory=valid_decision_for, model_id="deepseek:alpha")
    ids_a = await _draft_all_sections(env, outline_id, fake_a)
    service_a = _report_service(env, fake_a)
    report_a = await service_a.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(ids_a.values()))
    )
    assert report_a.replayed is False

    # 用不同 writer model 重写全部 sections → writer_input_fingerprint 不同 →
    # 全新草稿行（旧草稿保留，无 update）。
    fake_b = FakeDraftSectionModel(decision_factory=valid_decision_for, model_id="deepseek:beta")
    ids_b = await _draft_all_sections(env, outline_id, fake_b)
    assert all(ids_b[s] != ids_a[s] for s in ("S1", "S2", "S3"))

    service_b = _report_service(env, fake_b)
    report_b = await service_b.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(ids_b.values()))
    )
    assert report_b.report_id != report_a.report_id
    assert report_b.report_fingerprint != report_a.report_fingerprint
    assert report_b.replayed is False
    assert await _report_count(env["sessionmaker"]) == 2

    # 旧 Report 保留且仍可完整验证（immutable，无 update API）。
    await service_a.verify_report_integrity(report_a.report_id)


# ---------------------------------------------------------------- tamper → verify rejects


async def test_report_verify_rejects_tampered_payload(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )

    verified = await service.verify_report_integrity(report.report_id)
    corrupted = dict(verified.report_payload)
    corrupted["sections"] = [dict(s) for s in corrupted["sections"]]
    corrupted["sections"][0]["title"] = "被篡改的标题"
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE reports SET report_payload = CAST(:payload AS jsonb) WHERE report_id = :id"
            ).bindparams(payload=json.dumps(corrupted, ensure_ascii=False), id=report.report_id)
        )
        await session.commit()

    with pytest.raises(ReportIntegrityError) as excinfo:
        await service.verify_report_integrity(report.report_id)
    assert excinfo.value.code == "report_integrity_error"

    # checks 也拒绝（verify 先行），不创建 CheckResult。
    check_service = ReportCheckService(env["sessionmaker"], service)
    with pytest.raises(ReportIntegrityError):
        await check_service.run_report_checks(report.report_id)
    assert await _check_count(env["sessionmaker"]) == 0


async def test_report_verify_rejects_tampered_fingerprint(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("UPDATE reports SET report_fingerprint = :fp WHERE report_id = :id").bindparams(
                fp="f" * 64, id=report.report_id
            )
        )
        await session.commit()

    with pytest.raises(ReportIntegrityError) as excinfo:
        await service.verify_report_integrity(report.report_id)
    assert excinfo.value.code == "report_integrity_error"


async def test_report_verify_rejects_tampered_draft(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    # 篡改 S1 draft 的 section_fingerprint → 上游 DraftSection 完整性失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE draft_sections SET section_fingerprint = :fp WHERE draft_section_id = :id"
            ).bindparams(fp="1" * 64, id=draft_ids["S1"])
        )
        await session.commit()

    with pytest.raises(DraftSectionIntegrityError):
        await service.verify_report_integrity(report.report_id)


# ---------------------------------------------------------------- check replay / new check


async def test_report_check_replay_same_result(env, monkeypatch, connection_uri) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    draft_ids = await _draft_all_sections(
        env, outline_id, FakeDraftSectionModel(decision_factory=valid_decision_for)
    )
    service = _report_service(env, FakeDraftSectionModel(decision_factory=valid_decision_for))
    report = await service.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(draft_ids.values()))
    )
    check_service = ReportCheckService(env["sessionmaker"], service)

    first = await check_service.run_report_checks(report.report_id)
    assert first.replayed is False
    assert first.status == CHECK_STATUS_PASS

    second = await check_service.run_report_checks(report.report_id)
    assert second.check_result_id == first.check_result_id
    assert second.check_fingerprint == first.check_fingerprint
    assert second.replayed is True
    assert await _check_count(env["sessionmaker"]) == 1


async def test_report_change_new_check_result_old_preserved(
    env, monkeypatch, connection_uri
) -> None:
    outline_id = await _create_outline(env, monkeypatch, connection_uri, _two_theme_models())
    fake_a = FakeDraftSectionModel(decision_factory=valid_decision_for, model_id="deepseek:alpha")
    ids_a = await _draft_all_sections(env, outline_id, fake_a)
    service_a = _report_service(env, fake_a)
    report_a = await service_a.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(ids_a.values()))
    )
    check_a = await ReportCheckService(env["sessionmaker"], service_a).run_report_checks(
        report_a.report_id
    )
    assert check_a.replayed is False
    assert check_a.status == CHECK_STATUS_PASS

    # draft 变化 → 新 Report（新 report_fingerprint）→ 新 CheckResult（旧行保留）。
    fake_b = FakeDraftSectionModel(decision_factory=valid_decision_for, model_id="deepseek:beta")
    ids_b = await _draft_all_sections(env, outline_id, fake_b)
    service_b = _report_service(env, fake_b)
    report_b = await service_b.create_or_get_report(
        ReportAssemblyDraft(outline_id=outline_id, draft_section_ids=tuple(ids_b.values()))
    )
    check_b = await ReportCheckService(env["sessionmaker"], service_b).run_report_checks(
        report_b.report_id
    )

    assert check_b.check_result_id != check_a.check_result_id
    assert check_b.check_fingerprint != check_a.check_fingerprint
    assert check_b.replayed is False
    assert check_b.status == CHECK_STATUS_PASS
    assert await _check_count(env["sessionmaker"]) == 2
