"""Report export service integration tests (stage 6C spec H/I/J/M/N/O).

真实 PostgreSQL + 真实 LangGraph（PG Checkpointer）+ Fake LLM models，全程
**零真实 DeepSeek**。覆盖：

1. **资格判定（spec H）**：
   - 路径 A：audit pass / route pass → 可导出，audit_note=None；
   - 路径 B：audit fail / route human_review + 人工 approve → 可导出，
     audit_note=AUDIT_NOTE_HUMAN_APPROVED；
   - 不可导出：无 run / research 路由 → `ReportNotExportable`；非法 format →
     `ReportExportError`；
2. **渲染（spec O）**：markdown（UTF-8 + `[n]` 标记 + E1..En 附录 + audit_note）、
   docx（reopen-able）、pdf（%PDF + pdfplumber 中文可读）；
3. **replay（spec M）**：同输入 → 同 export_id + replayed=True + 仍 1 行；
   并发同输入 → 1 行；
4. **verify_export_integrity（spec N）**：happy 通过；篡改 content_sha256 /
   export_input_fingerprint / report_fingerprint / audit_fingerprint →
   `ReportExportIntegrityError`（只验证、不 repair）；
5. **task-scoped（spec P）**：get_export / get_export_content 用另一 task →
   `ReportExportNotFound`。
"""

import asyncio
import hashlib
from io import BytesIO

import pytest
import pytest_asyncio
from docx import Document
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import TaskArtifactIntegrityError
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.report_export.contracts import EXTENSION_BY_FORMAT, MEDIA_TYPE_BY_FORMAT
from app.report_export.errors import (
    ReportExportError,
    ReportExportIntegrityError,
    ReportExportNotFound,
    ReportNotExportable,
)
from app.report_export.pack import AUDIT_NOTE_HUMAN_APPROVED
from app.report_export.service import ReportExportService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_artifact_service import TaskArtifactService
from app.stage5.runner import Stage5WorkflowRunner
from app.storage.export_store import ExportArtifactStore
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from tests.audit.fakes import FakeAuditModel, pass_decision
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_report_audit_service import research_decision
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _seed_research_task
from tests.integration.test_stage5_workflow import (
    _request,
    _seed_synthesis,
    _stage5_deps,
)
from tests.integration.test_task_artifact_workspace import _run_full_chain_to_completed
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


def _make_export_service(sessionmaker, manager, deps, export_root) -> ReportExportService:
    artifact_service = TaskArtifactService.from_dependencies(sessionmaker, manager, deps)
    return ReportExportService(
        sessionmaker,
        artifact_service,
        report_service=deps.report_service,
        report_check_service=deps.report_check_service,
        report_audit_service=deps.report_audit_service,
        review_action_service=deps.review_action_service,
        company_service=CompanyIdentityService(sessionmaker),
        export_store=ExportArtifactStore(export_root),
    )


async def _chain_human_approved(env, monkeypatch, connection_uri):
    """路径 B：Stage4 → Stage5 waiting_human → approve → completed。"""
    manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    return manager, deps


async def _chain_audit_passed(env, monkeypatch, connection_uri):
    """路径 A：Stage4 → Stage5 finalize（audit pass / route pass）。"""
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=pass_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
    run = await runner.create_stage5_run(request)
    await runner.execute_stage5(run.run_id, request)
    return manager, deps


async def _chain_research_required(env, monkeypatch, connection_uri):
    """audit research 路由 → terminal research_required（不可导出）。"""
    synthesis_result_id = await _seed_synthesis(env, monkeypatch, connection_uri)
    request = _request(env, synthesis_result_id)
    deps = _stage5_deps(
        env["sessionmaker"],
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    runner = Stage5WorkflowRunner(env["sessionmaker"], manager, deps)
    run = await runner.create_stage5_run(request)
    await runner.execute_stage5(run.run_id, request)
    return manager, deps


async def _export_rows(sessionmaker) -> list[dict]:
    async with sessionmaker() as session:
        rows = (
            await session.execute(
                text("SELECT export_id, export_input_fingerprint FROM report_exports")
            )
        ).mappings()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------- eligibility (spec H)


async def test_export_human_approved_path_markdown(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """路径 B：audit fail + human approve → 可导出，audit_note 固定文案。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        result = await service.create_or_get_export(env["task_id"], "markdown")
        assert result.replayed is False
        assert result.format == "markdown"
        assert result.media_type == MEDIA_TYPE_BY_FORMAT["markdown"]
        assert result.byte_size > 0
        assert result.file_name.startswith("report_") and result.file_name.endswith(".md")

        record, stream = await service.get_export_content(env["task_id"], result.export_id)
        content = stream.read()
        stream.close()
        assert record.export_id == result.export_id
        assert record.byte_size == len(content)
        text_content = content.decode("utf-8")
        # 确定性导出：正文 + 引用标记 + 证据附录 + audit_note。
        assert "基本面研究报告" in text_content
        assert "[1]" in text_content, "段落应带引用编号标记"
        assert "证据附录" in text_content
        assert "### E1 ｜" in text_content, "附录 E1..En"
        assert AUDIT_NOTE_HUMAN_APPROVED in text_content, "人工批准路径必须带 audit_note"
    finally:
        await manager.close()


async def test_export_audit_pass_path_no_audit_note(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """路径 A：audit pass / route pass → 可导出，无 audit_note。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_audit_passed(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        result = await service.create_or_get_export(env["task_id"], "markdown")
        assert result.replayed is False
        record, stream = await service.get_export_content(env["task_id"], result.export_id)
        content = stream.read()
        stream.close()
        assert AUDIT_NOTE_HUMAN_APPROVED not in content.decode("utf-8")
    finally:
        await manager.close()


async def test_export_all_formats(env, monkeypatch, connection_uri, tmp_path) -> None:
    """markdown / docx / pdf 三种格式字节产出正确。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        for fmt, magic in (("markdown", None), ("docx", b"PK"), ("pdf", b"%PDF")):
            result = await service.create_or_get_export(env["task_id"], fmt)
            assert result.format == fmt
            assert result.media_type == MEDIA_TYPE_BY_FORMAT[fmt]
            assert result.byte_size > 0
            assert result.file_name.endswith(f".{EXTENSION_BY_FORMAT[fmt]}")
            record, stream = await service.get_export_content(env["task_id"], result.export_id)
            content = stream.read()
            stream.close()
            assert record.byte_size == len(content)
            if magic is not None:
                assert content[: len(magic)] == magic, f"{fmt} magic"
            else:
                assert content.decode("utf-8").startswith("# ")

        # DOCX reopen-able + 中文正文可读。
        docx_result = await service.create_or_get_export(env["task_id"], "docx")
        record, stream = await service.get_export_content(env["task_id"], docx_result.export_id)
        docx_bytes = stream.read()
        stream.close()
        document = Document(BytesIO(docx_bytes))
        texts = [p.text for p in document.paragraphs]
        assert any("基本面研究报告" in t for t in texts)
        assert "证据附录" in texts

        # PDF pdfplumber 提取真实中文。
        import pdfplumber

        pdf_result = await service.create_or_get_export(env["task_id"], "pdf")
        record, stream = await service.get_export_content(env["task_id"], pdf_result.export_id)
        pdf_bytes = stream.read()
        stream.close()
        with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
            extracted = "\n".join(page.extract_text() or "" for page in pdf.pages)
        assert "基本面研究报告" in extracted
    finally:
        await manager.close()


async def test_export_not_exportable_no_run(env, connection_uri, tmp_path) -> None:
    """无 Stage5 run（running / waiting_human / rewrite / failed 等状态）→ 409。"""
    sessionmaker = env["sessionmaker"]
    fresh_task = await _seed_research_task(sessionmaker)
    manager = LangGraphCheckpointManager(connection_uri)
    await manager.setup()
    try:
        deps = _stage5_deps(
            sessionmaker,
            draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
            audit_model=FakeAuditModel(decision_factory=pass_decision),
            revision_model=FakeRevisionWriterModel(),
        )
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        with pytest.raises(ReportNotExportable):
            await service.create_or_get_export(fresh_task, "markdown")
    finally:
        await manager.close()


async def test_export_not_exportable_research_route(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """audit research 路由 → terminal research_required → 不可导出。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_research_required(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        with pytest.raises(ReportNotExportable):
            await service.create_or_get_export(env["task_id"], "markdown")
    finally:
        await manager.close()


async def test_export_invalid_format(env, monkeypatch, connection_uri, tmp_path) -> None:
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        with pytest.raises(ReportExportError):
            await service.create_or_get_export(env["task_id"], "html")
    finally:
        await manager.close()


# ---------------------------------------------------------------- replay / concurrency (spec M)


async def test_export_replay_same_input(env, monkeypatch, connection_uri, tmp_path) -> None:
    """同输入 → 同 export_id + replayed=True + 仍 1 行（不重复渲染/建行）。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        first = await service.create_or_get_export(env["task_id"], "markdown")
        second = await service.create_or_get_export(env["task_id"], "markdown")
        assert first.replayed is False
        assert second.replayed is True
        assert second.export_id == first.export_id
        rows = await _export_rows(sessionmaker)
        assert len(rows) == 1, "同输入 → 只 1 行"
    finally:
        await manager.close()


async def test_export_concurrent_same_row(env, monkeypatch, connection_uri, tmp_path) -> None:
    """并发同输入 → 全部复用同一行（ON CONFLICT 保证 1 行）。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        results = await asyncio.gather(
            *[service.create_or_get_export(env["task_id"], "markdown") for _ in range(3)]
        )
        assert {r.export_id for r in results} == {results[0].export_id}
        rows = await _export_rows(sessionmaker)
        assert len(rows) == 1, "并发 → 1 行"
    finally:
        await manager.close()


# ---------------------------------------------------------------- verify_export_integrity (spec N)


async def _created_export(service, task_id, fmt="markdown"):
    result = await service.create_or_get_export(task_id, fmt)
    return result.export_id


async def test_verify_export_integrity_happy(env, monkeypatch, connection_uri, tmp_path) -> None:
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        export_id = await _created_export(service, env["task_id"])
        verified = await service.verify_export_integrity(export_id)
        assert verified.record.export_id == export_id
        assert verified.record.task_id == env["task_id"]
        assert verified.record.content_sha256
        assert verified.storage_key
    finally:
        await manager.close()


async def test_verify_export_integrity_tamper_content_sha256(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        export_id = await _created_export(service, env["task_id"])
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE report_exports SET content_sha256 = repeat('a', 64) "
                    "WHERE export_id = :id"
                ),
                {"id": export_id},
            )
            await session.commit()
        with pytest.raises(ReportExportIntegrityError):
            await service.verify_export_integrity(export_id)
    finally:
        await manager.close()


async def test_verify_export_integrity_tamper_fingerprint(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        export_id = await _created_export(service, env["task_id"])
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "UPDATE report_exports SET export_input_fingerprint = repeat('b', 64) "
                    "WHERE export_id = :id"
                ),
                {"id": export_id},
            )
            await session.commit()
        with pytest.raises(ReportExportIntegrityError):
            await service.verify_export_integrity(export_id)
    finally:
        await manager.close()


async def test_verify_export_integrity_tamper_report(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """tamper 报告 fingerprint → verify_report_integrity 失败 → integrity error。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        export_id = await _created_export(service, env["task_id"])
        async with sessionmaker() as session:
            await session.execute(text("UPDATE reports SET report_fingerprint = repeat('0', 64)"))
            await session.commit()
        with pytest.raises(ReportExportIntegrityError):
            await service.verify_export_integrity(export_id)
        # 只验证、不 repair：篡改不因 verify 被撤销。
        async with sessionmaker() as session:
            fp = (
                await session.execute(text("SELECT report_fingerprint FROM reports LIMIT 1"))
            ).scalar_one()
        assert fp == "0" * 64
    finally:
        await manager.close()


async def test_verify_export_integrity_tamper_audit(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        export_id = await _created_export(service, env["task_id"])
        async with sessionmaker() as session:
            await session.execute(
                text("UPDATE report_audits SET audit_fingerprint = repeat('0', 64)")
            )
            await session.commit()
        with pytest.raises(ReportExportIntegrityError):
            await service.verify_export_integrity(export_id)
    finally:
        await manager.close()


async def test_verify_export_integrity_after_tamper_not_repairable_by_replay(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """replay 不能“修复”损坏：tamper 后 create_or_get_export 因 verify 链
    （report）失败而抛 integrity error，绝无新行。

    `create_or_get_export` 走 canonical lineage（`TaskArtifactService.
    resolve_report`），report 篡改 → `TaskArtifactIntegrityError`（409）；
    export 自身 FK 独立重验的 `ReportExportIntegrityError` 只属于
    `verify_export_integrity`。两者都是「只验证、不 repair」。
    """
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        await _created_export(service, env["task_id"])
        before = len(await _export_rows(sessionmaker))
        async with sessionmaker() as session:
            await session.execute(text("UPDATE reports SET report_fingerprint = repeat('0', 64)"))
            await session.commit()
        # canonical lineage resolve_report 失败 → guarded integrity error（不降级）。
        with pytest.raises(TaskArtifactIntegrityError):
            await service.create_or_get_export(env["task_id"], "markdown")
        # 只验证、不 repair：无新行产生。
        after = await _export_rows(sessionmaker)
        assert len(after) == before
    finally:
        await manager.close()


# ---------------------------------------------------------------- task-scoped (spec P)


async def test_get_export_task_scoped_404(env, monkeypatch, connection_uri, tmp_path) -> None:
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        result = await service.create_or_get_export(env["task_id"], "markdown")
        other_task = await _seed_research_task(sessionmaker)
        with pytest.raises(ReportExportNotFound):
            await service.get_export(other_task, result.export_id)
        with pytest.raises(ReportExportNotFound):
            await service.get_export_content(other_task, result.export_id)
        # 本 task 正常读取。
        record = await service.get_export(env["task_id"], result.export_id)
        assert record.export_id == result.export_id
    finally:
        await manager.close()


async def test_export_content_bytes_match_archive(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """get_export_content 返回的字节与内容寻址归档一致（sha256 + byte_size）。"""
    sessionmaker = env["sessionmaker"]
    manager, deps = await _chain_human_approved(env, monkeypatch, connection_uri)
    try:
        service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
        result = await service.create_or_get_export(env["task_id"], "pdf")
        record, stream = await service.get_export_content(env["task_id"], result.export_id)
        content = stream.read()
        stream.close()
        assert hashlib.sha256(content).hexdigest() == record.content_sha256
        assert record.byte_size == len(content)
        assert record.media_type == MEDIA_TYPE_BY_FORMAT["pdf"]
        assert content[:4] == b"%PDF"
    finally:
        await manager.close()
