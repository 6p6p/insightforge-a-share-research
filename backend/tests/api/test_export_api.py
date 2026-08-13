"""Export web API tests (stage 6C spec P/T/U).

真实 PostgreSQL + 真实 lifespan（`app.router.lifespan_context`）+ httpx
ASGITransport，全程**零真实 DeepSeek**。完整 Stage4→Stage5→approve→completed
链之后验证：

1. **POST /tasks/{id}/export**：新建 → 201 + `X-Export-Replayed: false`；同输入
   → 200 + `X-Export-Replayed: true`（replay，spec M/P）；
2. **GET /tasks/{id}/exports/{export_id}**：metadata（task/report/format/
   content_sha256 等）；
3. **GET .../content**：200 + 正确 MIME + Content-Disposition attachment +
   字节内容；
4. 不可导出（无 run）→ 409 `report_not_exportable`；非法 format → 422；
   task-scoped 404 `report_export_not_found`；
5. **0-LLM（Gate0-F 风格）**：生产 DI（`create_stage5_dependencies`）模型全部换
   哨兵 + `ChatDeepSeek` 客户端禁止构造 → export POST 必须成功，证明导出路径
   **绝不初始化 LLM 客户端 / 不触碰模型**。
"""

import httpx
import pytest
import pytest_asyncio

from app.api.dependencies import get_report_export_service
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.main import create_app
from app.report_export.pack import AUDIT_NOTE_HUMAN_APPROVED
from app.report_export.service import ReportExportService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_artifact_service import TaskArtifactService
from app.stage5.dependencies import create_stage5_dependencies
from app.storage.export_store import ExportArtifactStore
from app.storage.raw_store import LocalRawArtifactStore
from tests.audit.fakes import FakeAuditModel
from tests.draft_section.fakes import FakeDraftSectionModel, valid_decision_for
from tests.integration.test_report_audit_service import research_decision
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _seed_research_task
from tests.integration.test_stage5_workflow import _stage5_deps
from tests.integration.test_task_artifact_workspace import _run_full_chain_to_completed
from tests.integration.test_valuation_claim_service import _seed_company
from tests.revision.fakes import FakeRevisionWriterModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


class _NoLLMSentinel:
    """Gate0-F 风格哨兵：只暴露只读 `model_id`；任何其他属性访问 → AssertionError。"""

    def __init__(self, settings, usage_observer=None) -> None:
        self._model_id = f"{settings.llm_provider}:{settings.llm_model}"

    @property
    def model_id(self) -> str:
        return self._model_id

    def __getattr__(self, name: str):
        raise AssertionError(f"export path must not use LLM model: {name}")


def _forbid_client(*args, **kwargs):
    raise AssertionError("export path must not initialize a DeepSeek client")


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


def _human_approved_deps(sessionmaker):
    return _stage5_deps(
        sessionmaker,
        draft_model=FakeDraftSectionModel(decision_factory=valid_decision_for),
        audit_model=FakeAuditModel(decision_factory=research_decision),
        revision_model=FakeRevisionWriterModel(),
    )


def _production_export_service(sessionmaker, manager, export_root) -> ReportExportService:
    """生产 DI 路径（哨兵模型已 patch）——证明导出 0 LLM。"""
    settings = get_settings()
    deps = create_stage5_dependencies(settings, sessionmaker)
    return _make_export_service(sessionmaker, manager, deps, export_root)


# ---------------------------------------------------------------- HTTP acceptance


async def test_export_http_create_replay_metadata_content(
    env, monkeypatch, connection_uri, tmp_path
) -> None:
    """POST 201 → replay 200 → metadata → content 下载。"""
    app = create_app(get_settings())
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    base = f"/api/v1/tasks/{task_id}"

    async with app.router.lifespan_context(app):
        manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
        try:
            deps = _human_approved_deps(sessionmaker)
            export_service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
            app.dependency_overrides[get_report_export_service] = lambda: export_service
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                # ---- 新建：201 + replay=false ----
                created = await client.post(f"{base}/export", json={"format": "markdown"})
                assert created.status_code == 201, created.text
                assert created.headers["x-export-replayed"] == "false"
                body = created.json()
                assert body["format"] == "markdown"
                assert body["replayed"] is False
                assert body["byte_size"] > 0
                export_id = body["export_id"]

                # ---- replay：200 + replay=true + 同一 export_id ----
                replayed = await client.post(f"{base}/export", json={"format": "markdown"})
                assert replayed.status_code == 200, replayed.text
                assert replayed.headers["x-export-replayed"] == "true"
                assert replayed.json()["export_id"] == export_id

                # ---- metadata ----
                meta = await client.get(f"{base}/exports/{export_id}")
                assert meta.status_code == 200, meta.text
                m = meta.json()
                assert m["task_id"] == str(task_id)
                assert m["report_id"]
                assert m["content_sha256"]
                assert m["file_name"].endswith(".md")

                # ---- content：MIME + Content-Disposition + 字节 ----
                content = await client.get(f"{base}/exports/{export_id}/content")
                assert content.status_code == 200, content.text
                assert content.headers["content-type"].startswith("text/markdown")
                assert 'attachment; filename="' in content.headers["content-disposition"]
                text_content = content.content.decode("utf-8")
                assert "基本面研究报告" in text_content
                assert AUDIT_NOTE_HUMAN_APPROVED in text_content  # 人工批准路径
        finally:
            await manager.close()


async def test_export_http_not_exportable_409(env, monkeypatch, connection_uri, tmp_path) -> None:
    app = create_app(get_settings())
    sessionmaker = env["sessionmaker"]
    async with app.router.lifespan_context(app):
        fresh_task = await _seed_research_task(sessionmaker)
        manager = None
        try:
            deps = _human_approved_deps(sessionmaker)
            # 无 run：export service 只用 checkpoint manager 读 state（空）→ 不依赖 manager。
            export_service = _make_export_service(sessionmaker, None, deps, tmp_path / "exports")
            app.dependency_overrides[get_report_export_service] = lambda: export_service
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(
                    f"/api/v1/tasks/{fresh_task}/export", json={"format": "markdown"}
                )
                assert resp.status_code == 409, resp.text
                error = resp.json()["error"]
                assert error["code"] == "report_not_exportable"
                assert isinstance(error["message"], str) and error["message"]
                assert isinstance(error["request_id"], str) and error["request_id"]
        finally:
            if manager is not None:
                await manager.close()


async def test_export_http_invalid_format_422(env, monkeypatch, connection_uri, tmp_path) -> None:
    app = create_app(get_settings())
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    async with app.router.lifespan_context(app):
        manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
        try:
            deps = _human_approved_deps(sessionmaker)
            export_service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
            app.dependency_overrides[get_report_export_service] = lambda: export_service
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                resp = await client.post(f"/api/v1/tasks/{task_id}/export", json={"format": "html"})
                assert resp.status_code == 422, resp.text
        finally:
            await manager.close()


async def test_export_http_task_scoped_404(env, monkeypatch, connection_uri, tmp_path) -> None:
    app = create_app(get_settings())
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    async with app.router.lifespan_context(app):
        manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
        try:
            deps = _human_approved_deps(sessionmaker)
            export_service = _make_export_service(sessionmaker, manager, deps, tmp_path / "exports")
            app.dependency_overrides[get_report_export_service] = lambda: export_service
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(
                    f"/api/v1/tasks/{task_id}/export", json={"format": "markdown"}
                )
                assert created.status_code == 201, created.text
                export_id = created.json()["export_id"]
                other_task = await _seed_research_task(sessionmaker)
                # metadata：不属于该 task → 404。
                meta = await client.get(f"/api/v1/tasks/{other_task}/exports/{export_id}")
                assert meta.status_code == 404, meta.text
                assert meta.json()["error"]["code"] == "report_export_not_found"
                # content：不属于该 task → 404。
                content = await client.get(
                    f"/api/v1/tasks/{other_task}/exports/{export_id}/content"
                )
                assert content.status_code == 404, content.text
        finally:
            await manager.close()


async def test_export_http_zero_llm(env, monkeypatch, connection_uri, tmp_path) -> None:
    """0-LLM 证明：生产 DI 模型全部换哨兵 + ChatDeepSeek 禁止 → export POST 成功。"""
    monkeypatch.setattr(
        "app.draft_section.factory.create_draft_section_model",
        lambda settings, usage_observer=None: _NoLLMSentinel(settings),
    )
    monkeypatch.setattr(
        "app.revision.factory.create_revision_writer_model",
        lambda settings, usage_observer=None: _NoLLMSentinel(settings),
    )
    monkeypatch.setattr("app.audit.adapters.DeepSeekAuditModel", _NoLLMSentinel)
    monkeypatch.setattr("langchain_deepseek.ChatDeepSeek", _forbid_client)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)

    app = create_app(get_settings())
    sessionmaker = env["sessionmaker"]
    task_id = env["task_id"]
    base = f"/api/v1/tasks/{task_id}"

    async with app.router.lifespan_context(app):
        manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
        try:
            # 生产 DI（哨兵已 patch）：任何 LLM 客户端 / 模型属性触碰都会抛错。
            export_service = _production_export_service(sessionmaker, manager, tmp_path / "exports")
            app.dependency_overrides[get_report_export_service] = lambda: export_service
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
                created = await client.post(f"{base}/export", json={"format": "markdown"})
                assert created.status_code == 201, created.text
                assert created.json()["replayed"] is False
                content = await client.get(f"{base}/exports/{created.json()['export_id']}/content")
                assert content.status_code == 200, content.text
        finally:
            await manager.close()
