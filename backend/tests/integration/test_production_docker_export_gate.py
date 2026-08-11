"""Production Docker export gate (stage 6C spec C/S): nginx same-origin export smoke.

前置条件：`docker compose up -d --build` 四服务（postgres/chroma/backend/frontend）
已就绪；backend healthy、frontend running。测试经 **nginx 生产 origin**
（`http://127.0.0.1:8080`，frontend 服务端口映射）实际验证：

1. `POST /api/v1/tasks/{task_id}/export`（markdown / docx / pdf）× 3 格式 →
   下载 content → Markdown（200 + text/markdown + 中文正文 + 人工批准 audit note）、
   DOCX（PK ZIP magic + python-docx 重开 + 中文）、PDF（%PDF magic + pdfplumber
   提取中文标题/正文），各自 Content-Disposition attachment + 扩展名正确；
2. `GET /api/v1/health/live` × 5 与 `/ready` × 5 均 200（ready 含
   export_storage 探针 = ok）；
3. SSE smoke：`GET /tasks/{id}/events` 经 nginx 不 502/404、content-type 为
   text/event-stream（proxy_buffering off 生效）。

Seed 用**正式 services + 真实 PostgreSQL + Fake LLM models** 跑真实 Stage4→Stage5
→approve→completed 全链（0 real DeepSeek）；**不手工伪造 ReportExport 行**——
导出行由生产容器 backend 经 `create_or_get_export` 真实创建。全程 0 LLM /
0 Retrieval / 0 Chroma query / 0 Web fetch。
"""

from io import BytesIO

import httpx
import pytest
import pytest_asyncio
from docx import Document

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.report_export.pack import AUDIT_NOTE_HUMAN_APPROVED
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_stage4_workflow import _seed_research_task
from tests.integration.test_task_artifact_workspace import _run_full_chain_to_completed
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

# frontend 服务宿主端口（compose.yaml `ports: "8080:80"`）。nginx 同源反代
# `/api/v1` → backend 容器 8000。
_PRODUCTION_ORIGIN = "http://127.0.0.1:8080"


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


async def _preflight_nginx() -> None:
    """gate 前置：生产 nginx 必须可达，否则明确失败（而非静默跳过）。"""
    async with httpx.AsyncClient(timeout=10) as client:
        try:
            response = await client.get(f"{_PRODUCTION_ORIGIN}/api/v1/health/live")
        except httpx.ConnectError as exc:
            pytest.fail(
                "生产 nginx 不可达——请先执行 `docker compose up -d --build` "
                f"并等待 backend healthy / frontend running：{exc}"
            )
        assert response.status_code == 200, response.text


async def _download_export(
    client: httpx.AsyncClient, task_id: str, format: str
) -> tuple[int, str, bytes]:
    """POST export → 下载 content → 返回 (content-status, content-disposition, bytes)。"""
    created = await client.post(
        f"{_PRODUCTION_ORIGIN}/api/v1/tasks/{task_id}/export", json={"format": format}
    )
    assert created.status_code in (200, 201), f"{format}: {created.text}"
    export_id = created.json()["export_id"]
    content = await client.get(
        f"{_PRODUCTION_ORIGIN}/api/v1/tasks/{task_id}/exports/{export_id}/content"
    )
    assert content.status_code == 200, f"{format} download: {content.text}"
    return content.status_code, content.headers["content-disposition"], content.content


# ---------------------------------------------------------------- gate tests


async def test_production_nginx_export_markdown_docx_pdf(env, monkeypatch, connection_uri) -> None:
    """经 nginx 生产 origin 真实下载三种 Export，校验 MIME/魔数/中文/Content-Disposition。"""
    await _preflight_nginx()
    task_id = str(env["task_id"])
    manager, _ = await _run_full_chain_to_completed(env, monkeypatch, connection_uri)
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            # ---- markdown：200 + text/markdown + 中文正文 + 人工批准 audit note ----
            status, cd, md_bytes = await _download_export(client, task_id, "markdown")
            assert status == 200
            assert "attachment" in cd and ".md" in cd, cd
            md_text = md_bytes.decode("utf-8")
            assert "基本面研究报告" in md_text
            assert "研究问题" in md_text
            assert AUDIT_NOTE_HUMAN_APPROVED in md_text  # human approve 路径审计注记
            assert "[1]" in md_text  # 确定性引用标记

            # ---- docx：PK magic + python-docx 重开 + 中文正文 ----
            status, cd, docx_bytes = await _download_export(client, task_id, "docx")
            assert status == 200
            assert "attachment" in cd and ".docx" in cd, cd
            assert docx_bytes[:2] == b"PK", "DOCX 是 ZIP 容器"
            document = Document(BytesIO(docx_bytes))
            texts = [p.text for p in document.paragraphs]
            assert any("基本面研究报告" in t for t in texts)
            assert "证据附录" in texts

            # ---- pdf：%PDF magic + pdfplumber 提取中文标题/正文 ----
            status, cd, pdf_bytes = await _download_export(client, task_id, "pdf")
            assert status == 200
            assert "attachment" in cd and ".pdf" in cd, cd
            assert pdf_bytes[:4] == b"%PDF", "PDF magic"
            import pdfplumber

            with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
                extracted = "\n".join(page.extract_text() or "" for page in pdf.pages)
            assert "基本面研究报告" in extracted
            assert "研究问题" in extracted

            # ---- SSE smoke：经 nginx 事件流不 502/404，content-type 正确 ----
            async with client.stream(
                "GET", f"{_PRODUCTION_ORIGIN}/api/v1/tasks/{task_id}/events", timeout=15
            ) as stream:
                assert stream.status_code == 200, stream.status_code
                ctype = stream.headers.get("content-type", "")
                assert "text/event-stream" in ctype, ctype
                first_lines: list[str] = []
                async for line in stream.aiter_lines():
                    first_lines.append(line)
                    if len(first_lines) >= 3:
                        break
                assert first_lines, "SSE 流应有初始事件/重放（completed run 有历史）"
    finally:
        await manager.close()


async def test_production_nginx_health_live_ready_5x200() -> None:
    """live / ready 各连续 5 次 200（经 nginx 反代），ready 含 export_storage 探针 ok。"""
    await _preflight_nginx()
    async with httpx.AsyncClient(timeout=30) as client:
        for _ in range(5):
            live = await client.get(f"{_PRODUCTION_ORIGIN}/api/v1/health/live")
            assert live.status_code == 200, live.text
        for _ in range(5):
            ready = await client.get(f"{_PRODUCTION_ORIGIN}/api/v1/health/ready")
            assert ready.status_code == 200, ready.text
            checks = ready.json()["checks"]
            assert checks["export_storage"] == "ok", checks
            for name, value in checks.items():
                assert value == "ok", f"{name} 探针非 ok：{checks}"
