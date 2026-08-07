"""真实链路验收：text/html SourceRecord 内容端点 HTTP 415 + XSS 防护 + stream 生命周期。

需要真实 PostgreSQL（127.0.0.1:5433）。真实 LocalRawArtifactStore + 真实
SourceIngestionService + 真实 FastAPI（create_app 构建的真实路由/中间件/异常
处理器），不注入 Fake Service。零真实网络（conftest autouse guard 兜底）。

- §四：news_article（text/html）SourceRecord 的 GET /content → HTTP 415 +
  error.code=source_content_unsupported_media_type + XSS marker 不进响应体 +
  Content-Type 非 text/html；
- §五：HTML 路径 stream 被显式 close（stream.closed=True，无句柄泄漏）；
  PDF 路径仍作为 StreamingResponse 完整返回字节（流未被提前关闭）。

事件循环说明：pytest-asyncio 测试与 async fixture 共用一个事件循环；
httpx.ASGITransport 在该循环内直接调 ASGI app，避免 TestClient 独立 portal
造成的跨循环复用 psycopg 连接（psycopg async 连接绑定创建它的循环）。
"""

import io
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.api.dependencies import get_source_ingestion_service
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.main import create_app
from app.repositories.company_repository import CompanyRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.source_ingestion_service import SourceIngestionService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_XSS_MARKER = "<script>window.__INSIGHTFORGE_XSS_TEST__=1</script>"
_HTML = f"<html><head><title>新闻</title></head><body>{_XSS_MARKER}</body></html>".encode()
_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_XINHUA_URL = "https://www.xinhuanet.com/2026/0807/0001.htm"


class _CapturingIngestionService(SourceIngestionService):
    """真实 Service，仅额外记录每次 open_source_content 返回的底层 stream。

    用于 §五 断言：HTML 路径 415 后 stream 已关闭；PDF 路径流式响应期间
    未被提前关闭（读取完整字节）且迭代结束后关闭。
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.opened_streams: list[io.BufferedReader] = []

    async def open_source_content(self, source_id):
        record, stream = await super().open_source_content(source_id)
        self.opened_streams.append(stream)
        return record, stream


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


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM news_source_verifications"))
        await session.execute(text("DELETE FROM news_discovery_candidates"))
        await session.execute(text("DELETE FROM news_discovery_runs"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    # 确保 xinhuanet / sse 等默认 Provider 存在（upsert，不破坏其他测试）。
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = uuid4()
    async with sessionmaker() as session:
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code="600519",
                identity_key="SSE:600519",
                board="sse_main",
                official_name="测试公司",
                short_name="测试",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "raw_root": raw_root,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _create_record(env: dict, *, media_type: str) -> tuple[SourceRecordModel, str]:
    """真实 LocalRawArtifactStore 落盘 + 真实 Repository 登记 SourceRecord。

    media_type="text/html" → news_article（xinhuanet）；"application/pdf" →
    annual_report（sse）。返回 (record, storage_key)。
    """
    is_html = media_type == "text/html"
    if is_html:
        stored = env["raw_store"].put_html_bytes(_HTML)
    else:
        stored = env["raw_store"].put_pdf_stream(io.BytesIO(_PDF))
    async with env["sessionmaker"]() as session:
        artifact = await RawArtifactRepository(session).create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        if artifact is None:  # 并发/残留冲突：复用既有行
            artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
            assert artifact is not None
        record = SourceRecordModel(
            company_id=env["company_id"],
            provider_key="xinhuanet" if is_html else "sse",
            artifact_id=artifact.artifact_id,
            document_type="news_article" if is_html else "annual_report",
            title="新闻标题" if is_html else "2024 年年度报告",
            source_url=_XINHUA_URL if is_html else "https://www.sse.com.cn/2024/000001.pdf",
            acquisition_method="public_html" if is_html else "user_upload",
            status="available",
            authority_tier_snapshot=3 if is_html else 1,
            critical_claim_eligible_snapshot=False if is_html else True,
            provider_capabilities_snapshot=(
                ["news_article"] if is_html else ["company_announcement", "document_download"]
            ),
            acquired_at=datetime.now(UTC),
        )
        record = await SourceRecordRepository(session).create(record)
        await session.commit()
        return record, stored.storage_key


@pytest_asyncio.fixture
async def real_app(env) -> tuple:
    """真实 FastAPI（create_app）+ 真实 Service，ASGI transport 同一事件循环。

    不进入 app lifespan（不创建 workflow/chroma/checkpoint 资源），只走真实
    路由、真实中间件、真实异常处理器与真实 Service/DB/Store 链路。
    """
    service = _CapturingIngestionService(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        max_bytes=1024 * 1024,
    )
    application = create_app(get_settings())
    application.dependency_overrides[get_source_ingestion_service] = lambda: service
    transport = httpx.ASGITransport(app=application)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        yield client, service


# ---------------------------------------------------------------- §四 真实 415


async def test_html_news_article_content_returns_415_no_xss(real_app, env) -> None:
    client, service = real_app
    record, storage_key = await _create_record(env, media_type="text/html")

    # sanity：XSS marker 确实已归档进 raw store——本测试验证的是"内容端点拒绝
    # 内联返回"，而不是"HTML 从未入库"。
    stored_path = env["raw_root"] / storage_key
    assert stored_path.is_file()
    assert _XSS_MARKER.encode() in stored_path.read_bytes()

    response = await client.get(f"/api/v1/source-records/{record.source_id}/content")

    # HTTP 415 + 统一 error.code
    assert response.status_code == 415
    body = response.json()
    assert body["error"]["code"] == "source_content_unsupported_media_type"
    assert body["error"]["message"] == "该来源媒体类型不支持内容下载"

    # XSS marker 绝不进入响应体（响应是 JSON error envelope，不内联第三方 HTML）
    assert _XSS_MARKER.encode() not in response.content
    assert _XSS_MARKER not in response.text
    # Content-Type 不得是 text/html（不能把原始 HTML 当可执行页面返回）
    assert "text/html" not in response.headers.get("content-type", "")

    # §五：HTML 路径 stream 已被显式 close，无句柄泄漏
    assert len(service.opened_streams) == 1
    assert service.opened_streams[0].closed is True


# ---------------------------------------------------------------- §五 stream 生命周期


async def test_pdf_content_streams_as_streaming_response(real_app, env) -> None:
    client, service = real_app
    record, _ = await _create_record(env, media_type="application/pdf")

    response = await client.get(f"/api/v1/source-records/{record.source_id}/content")

    # PDF 仍走 StreamingResponse，未被提前关闭
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/pdf"
    assert response.headers["content-length"] == str(len(_PDF))
    assert response.headers["content-disposition"] == (
        f'attachment; filename="source-{record.source_id}.pdf"'
    )
    # 流未被提前关闭：完整迭代出全部字节（若提前 close，read 返回空）
    assert response.content == _PDF

    # 迭代结束后 finally 关闭流，无泄漏
    assert len(service.opened_streams) == 1
    assert service.opened_streams[0].closed is True
