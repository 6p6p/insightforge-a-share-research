"""Integration tests for source ingestion: raw artifact + source record + store.

需要真实 PostgreSQL（127.0.0.1:5433）。URL 导入使用 httpx MockTransport，
不访问外网。覆盖 SHA-256 去重、约束、上传/URL 导入、查询与内容读回。
"""

import hashlib
import io
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.acquisition.http_fetcher import SafePdfFetcher
from app.core.config import get_settings
from app.core.errors import (
    CompanyIdentityNotFound,
    InvalidPdfFile,
    SourceCapabilityNotAllowed,
    SourceDownloadFailed,
    SourceFileTooLarge,
    SourceProviderDisabled,
    SourceUrlNotAllowed,
)
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.domain.source_records import SourceDocumentType
from app.repositories.company_repository import CompanyRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.source_ingestion_service import SourceIngestionService
from app.storage.raw_store import LocalRawArtifactStore

pytestmark = pytest.mark.integration

configure_asyncio_runtime()


_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_PDF_OTHER = b"%PDF-1.7\n%% another report\n%%EOF\n"
_SOURCE_URL = "https://www.sse.com.cn/2024/000001.pdf"
_DEFAULT_PROVIDER_KEYS = ("sse", "szse", "bse", "cninfo", "csrc", "nbs", "fred", "world_bank")


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


def _provider(provider_key: str, **overrides: object) -> SourceProviderModel:
    defaults: dict = {
        "provider_key": provider_key,
        "display_name": provider_key,
        "provider_type": "exchange",
        "authority_tier": 1,
        "homepage_url": "https://www.sse.com.cn",
        "allowed_domains": ["sse.com.cn"],
        "capabilities": ["company_announcement", "document_download"],
        "acquisition_methods": ["official_web_page"],
        "exchange_scope": ["SSE"],
        "requires_api_key": False,
        "critical_claim_eligible": True,
        "enabled": True,
    }
    defaults.update(overrides)
    return SourceProviderModel(**defaults)


def _company(provider_key: str, **overrides: object) -> CompanyModel:
    defaults: dict = {
        "company_id": uuid4(),
        "exchange": "SSE",
        "security_code": "600519",
        "identity_key": "SSE:600519",
        "board": "sse_main",
        "official_name": "测试公司",
        "short_name": "测试",
        "listing_status": "listed",
        "identity_source_provider_key": provider_key,
        "identity_source_url": "https://www.sse.com.cn",
    }
    defaults.update(overrides)
    return CompanyModel(**defaults)


def _record(company_id, artifact_id, **overrides: object) -> SourceRecordModel:
    defaults: dict = {
        "company_id": company_id,
        "provider_key": "sse",
        "artifact_id": artifact_id,
        "document_type": "annual_report",
        "title": "2024 年年度报告",
        "source_url": _SOURCE_URL,
        "acquisition_method": "user_upload",
        "status": "available",
        "authority_tier_snapshot": 1,
        "critical_claim_eligible_snapshot": True,
        "provider_capabilities_snapshot": ["company_announcement", "document_download"],
    }
    defaults.update(overrides)
    return SourceRecordModel(**defaults)


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        placeholders = ",".join(f"'{key}'" for key in _DEFAULT_PROVIDER_KEYS)
        await session.execute(
            text(f"DELETE FROM source_providers WHERE provider_key NOT IN ({placeholders})")
        )
        await session.commit()


async def _create_artifact(sessionmaker, store, content: bytes) -> RawArtifactModel:
    stored = store.put_pdf_stream(io.BytesIO(content))
    async with sessionmaker() as session:
        repo = RawArtifactRepository(session)
        artifact = await repo.create(
            RawArtifactModel(
                content_sha256=stored.content_sha256,
                storage_key=stored.storage_key,
                byte_size=stored.byte_size,
                media_type=stored.media_type,
            )
        )
        await session.commit()
        assert artifact is not None
        return artifact


@pytest_asyncio.fixture
async def ingest_env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(
            _provider("sse", critical_claim_eligible=True)
        )
        await session.commit()
    company = _company("sse")
    async with sessionmaker() as session:
        await CompanyRepository(session).create(company)
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "raw_root": raw_root,
        "company_id": company.company_id,
        "provider_key": "sse",
    }
    await _cleanup(sessionmaker)


def _forbidden_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError("URL import test must inject a MockTransport; refusing real network")


def _service(
    env: dict,
    *,
    fetcher: SafePdfFetcher | None = None,
    max_bytes: int = 1024 * 1024,
) -> SourceIngestionService:
    # 默认 fetcher 是永不让步的 MockTransport：URL 导入忘记注入 MockTransport
    # 会立即失败，而不是回退到真实 fetcher（真实网络在测试中已被 guard 禁止）。
    if fetcher is None:
        fetcher = SafePdfFetcher(transport=httpx.MockTransport(_forbidden_handler))
    return SourceIngestionService(
        sessionmaker=env["sessionmaker"],
        raw_store=env["raw_store"],
        fetcher=fetcher,
        max_bytes=max_bytes,
    )


def _upload_kwargs(env: dict, **overrides: object) -> dict:
    kwargs: dict = {
        "company_id": env["company_id"],
        "provider_key": env["provider_key"],
        "document_type": SourceDocumentType.ANNUAL_REPORT,
        "title": "2024 年年度报告",
        "source_url": _SOURCE_URL,
        "published_at": None,
        "reporting_period_end": None,
        "external_document_id": None,
    }
    kwargs.update(overrides)
    return kwargs


# --------------------------------------------------------------- tables / ddl


@pytest.mark.asyncio
async def test_raw_artifact_and_source_record_tables_exist(database, sessionmaker) -> None:
    async with sessionmaker() as session:
        result = await session.execute(
            text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema='public' AND table_name IN "
                "('raw_artifacts','source_records')"
            )
        )
        tables = {row[0] for row in result}
    assert {"raw_artifacts", "source_records"} <= tables


@pytest.mark.asyncio
async def test_raw_artifact_constraints_enforced(ingest_env) -> None:
    env = ingest_env
    async with env["sessionmaker"]() as session:
        repo = RawArtifactRepository(session)
        with pytest.raises(IntegrityError):
            await repo.create(
                RawArtifactModel(
                    content_sha256="short",
                    storage_key="sha256/ab/cd/x.pdf",
                    byte_size=10,
                    media_type="application/pdf",
                )
            )
        await session.rollback()
        with pytest.raises(IntegrityError):
            await repo.create(
                RawArtifactModel(
                    content_sha256="a" * 64,
                    storage_key="sha256/ab/cd/x.pdf",
                    byte_size=0,
                    media_type="application/pdf",
                )
            )
        await session.rollback()
        with pytest.raises(IntegrityError):
            await repo.create(
                RawArtifactModel(
                    content_sha256="b" * 64,
                    storage_key="sha256/ab/cd/y.pdf",
                    byte_size=10,
                    media_type="application/octet-stream",
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_source_record_constraints_enforced(ingest_env) -> None:
    env = ingest_env
    artifact = await _create_artifact(env["sessionmaker"], env["raw_store"], _PDF)
    async with env["sessionmaker"]() as session:
        repo = SourceRecordRepository(session)
        with pytest.raises(IntegrityError):
            await repo.create(_record(env["company_id"], artifact.artifact_id, title="   "))
        await session.rollback()
        with pytest.raises(IntegrityError):
            await repo.create(
                _record(
                    env["company_id"],
                    artifact.artifact_id,
                    source_url="http://www.sse.com.cn/a.pdf",
                )
            )
        await session.rollback()
        with pytest.raises(IntegrityError):
            await repo.create(
                _record(env["company_id"], artifact.artifact_id, authority_tier_snapshot=5)
            )
        await session.rollback()


# ------------------------------------------------------------------ upload


@pytest.mark.asyncio
async def test_upload_creates_artifact_and_record(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    result = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    assert result.replayed is False
    record = result.record
    assert record.company_id == env["company_id"]
    assert record.provider_key == "sse"
    assert record.document_type == SourceDocumentType.ANNUAL_REPORT
    assert record.acquisition_method == "user_upload"
    assert record.authority_tier_snapshot == 1
    assert record.critical_claim_eligible_snapshot is True
    assert record.provider_capabilities_snapshot == ["company_announcement", "document_download"]
    assert record.status == "available"
    assert record.byte_size == len(_PDF)
    assert record.media_type == "application/pdf"
    assert record.content_sha256 == hashlib.sha256(_PDF).hexdigest()
    expected_key = (
        f"sha256/{record.content_sha256[:2]}/{record.content_sha256[2:4]}"
        f"/{record.content_sha256}.pdf"
    )
    assert env["raw_store"].exists(expected_key)


@pytest.mark.asyncio
async def test_upload_same_content_dedupes_artifact(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    first = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    second = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    assert second.replayed is True
    assert second.record.source_id == first.record.source_id
    assert second.record.artifact_id == first.record.artifact_id
    async with env["sessionmaker"]() as session:
        result = await session.execute(text("SELECT count(*) FROM raw_artifacts"))
        assert result.scalar_one() == 1
        result = await session.execute(text("SELECT count(*) FROM source_records"))
        assert result.scalar_one() == 1


@pytest.mark.asyncio
async def test_upload_same_content_different_url_shared_artifact(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    first = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    second = await service.ingest_upload(
        **_upload_kwargs(env, source_url="https://www.sse.com.cn/2024/000001_dup.pdf"),
        stream=io.BytesIO(_PDF),
    )
    assert first.record.artifact_id == second.record.artifact_id
    assert first.record.source_id != second.record.source_id
    async with env["sessionmaker"]() as session:
        result = await session.execute(text("SELECT count(*) FROM raw_artifacts"))
        assert result.scalar_one() == 1
        result = await session.execute(text("SELECT count(*) FROM source_records"))
        assert result.scalar_one() == 2


@pytest.mark.asyncio
async def test_upload_invalid_pdf_rejected(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    with pytest.raises(InvalidPdfFile):
        await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(b"not a pdf at all"))


@pytest.mark.asyncio
async def test_upload_oversized_rejected(ingest_env) -> None:
    env = ingest_env
    small_store = LocalRawArtifactStore(root=env["raw_root"] / "small", max_bytes=32)
    service = SourceIngestionService(
        sessionmaker=env["sessionmaker"], raw_store=small_store, max_bytes=32
    )
    with pytest.raises(SourceFileTooLarge):
        await service.ingest_upload(
            **_upload_kwargs(env), stream=io.BytesIO(b"%PDF-1.7\n" + b"x" * 64)
        )


@pytest.mark.asyncio
async def test_upload_company_not_found_rejected(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    with pytest.raises(CompanyIdentityNotFound):
        await service.ingest_upload(
            **_upload_kwargs(env, company_id=uuid4()), stream=io.BytesIO(_PDF)
        )


@pytest.mark.asyncio
async def test_upload_disabled_provider_rejected(ingest_env) -> None:
    env = ingest_env
    async with env["sessionmaker"]() as session:
        await SourceProviderRepository(session).upsert(
            _provider("disabled_provider", enabled=False)
        )
        await session.commit()
    service = _service(env)
    with pytest.raises(SourceProviderDisabled):
        await service.ingest_upload(
            **_upload_kwargs(env, provider_key="disabled_provider"),
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_upload_provider_without_download_capability_rejected(ingest_env) -> None:
    env = ingest_env
    async with env["sessionmaker"]() as session:
        await SourceProviderRepository(session).upsert(
            _provider("macro_provider", capabilities=["macro_data"])
        )
        await session.commit()
    service = _service(env)
    with pytest.raises(SourceCapabilityNotAllowed):
        await service.ingest_upload(
            **_upload_kwargs(env, provider_key="macro_provider"),
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_upload_url_outside_allowed_domains_rejected(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    with pytest.raises(SourceUrlNotAllowed):
        await service.ingest_upload(
            **_upload_kwargs(env, source_url="https://evil.example.org/a.pdf"),
            stream=io.BytesIO(_PDF),
        )


# -------------------------------------------------------------- import-url


def _pdf_handler(content: bytes = _PDF, *, status: int = 200, headers: dict | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=content,
            headers=headers
            or {
                "content-type": "application/pdf",
                "content-length": str(len(content)),
            },
        )

    return handler


@pytest.mark.asyncio
async def test_import_url_success(ingest_env) -> None:
    env = ingest_env
    service = _service(env, fetcher=SafePdfFetcher(transport=httpx.MockTransport(_pdf_handler())))
    result = await service.ingest_url(**_upload_kwargs(env))
    assert result.replayed is False
    record = result.record
    assert record.acquisition_method == "user_provided_url"
    assert record.byte_size == len(_PDF)
    assert record.source_url == _SOURCE_URL


@pytest.mark.asyncio
async def test_import_url_redirect_within_domain(ingest_env) -> None:
    env = ingest_env

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/final.pdf"}, content=b"")
        return httpx.Response(200, content=_PDF, headers={"content-length": str(len(_PDF))})

    service = _service(env, fetcher=SafePdfFetcher(transport=httpx.MockTransport(handler)))
    result = await service.ingest_url(
        **_upload_kwargs(env, source_url="https://www.sse.com.cn/redirect")
    )
    assert result.replayed is False
    assert result.record.acquisition_method == "user_provided_url"


@pytest.mark.asyncio
async def test_import_url_redirect_to_disallowed_domain_rejected(ingest_env) -> None:
    env = ingest_env

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "https://evil.example.org/a.pdf"},
            content=b"",
        )

    service = _service(env, fetcher=SafePdfFetcher(transport=httpx.MockTransport(handler)))
    with pytest.raises(SourceUrlNotAllowed):
        await service.ingest_url(**_upload_kwargs(env))


@pytest.mark.asyncio
async def test_import_url_content_length_over_limit_rejected(ingest_env) -> None:
    env = ingest_env
    service = _service(
        env,
        fetcher=SafePdfFetcher(
            transport=httpx.MockTransport(_pdf_handler(headers={"content-length": "999999999"}))
        ),
        max_bytes=1024,
    )
    with pytest.raises(SourceFileTooLarge):
        await service.ingest_url(**_upload_kwargs(env))


@pytest.mark.asyncio
async def test_import_url_server_error_rejected(ingest_env) -> None:
    env = ingest_env
    service = _service(
        env, fetcher=SafePdfFetcher(transport=httpx.MockTransport(_pdf_handler(status=500)))
    )
    with pytest.raises(SourceDownloadFailed):
        await service.ingest_url(**_upload_kwargs(env))


# ------------------------------------------------------------------- queries


@pytest.mark.asyncio
async def test_get_source_returns_full_record(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    created = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    fetched = await service.get_source(created.record.source_id)
    assert fetched.source_id == created.record.source_id
    assert fetched.content_sha256 == created.record.content_sha256
    assert fetched.byte_size == len(_PDF)


@pytest.mark.asyncio
async def test_list_company_sources_filter_and_pagination(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    await service.ingest_upload(
        **_upload_kwargs(env),
        stream=io.BytesIO(_PDF),
    )
    await service.ingest_upload(
        **_upload_kwargs(
            env,
            document_type=SourceDocumentType.COMPANY_ANNOUNCEMENT,
            source_url="https://www.sse.com.cn/2024/announcement.pdf",
        ),
        stream=io.BytesIO(_PDF_OTHER),
    )

    all_records = await service.list_company_sources(env["company_id"], None, 50, 0)
    assert all_records.total == 2
    assert len(all_records.items) == 2

    filtered = await service.list_company_sources(
        env["company_id"], SourceDocumentType.COMPANY_ANNOUNCEMENT, 50, 0
    )
    assert filtered.total == 1
    assert filtered.items[0].document_type == SourceDocumentType.COMPANY_ANNOUNCEMENT

    paged = await service.list_company_sources(env["company_id"], None, 1, 0)
    assert paged.total == 2
    assert len(paged.items) == 1


@pytest.mark.asyncio
async def test_open_source_content_returns_original_bytes(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    created = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    record, stream = await service.open_source_content(created.record.source_id)
    try:
        assert stream.read() == _PDF
    finally:
        stream.close()
    assert record.byte_size == len(_PDF)


@pytest.mark.asyncio
async def test_replay_returns_existing_record(ingest_env) -> None:
    env = ingest_env
    # ingest_url 需先下载才能获得 sha256 判重，必须使用 MockTransport，禁止访问外网。
    service = _service(env, fetcher=SafePdfFetcher(transport=httpx.MockTransport(_pdf_handler())))
    first = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    replay = await service.ingest_url(**_upload_kwargs(env))
    assert replay.replayed is True
    assert replay.record.source_id == first.record.source_id
    assert replay.record.artifact_id == first.record.artifact_id


# -------------------------------------------------- capabilities snapshot


@pytest.mark.asyncio
async def test_import_url_saves_same_capabilities_snapshot(ingest_env) -> None:
    env = ingest_env
    service = _service(env, fetcher=SafePdfFetcher(transport=httpx.MockTransport(_pdf_handler())))
    result = await service.ingest_url(**_upload_kwargs(env))
    assert result.record.acquisition_method == "user_provided_url"
    assert result.record.provider_capabilities_snapshot == [
        "company_announcement",
        "document_download",
    ]


@pytest.mark.asyncio
async def test_provider_capabilities_change_does_not_alter_snapshot(ingest_env) -> None:
    env = ingest_env
    service = _service(env)
    created = await service.ingest_upload(**_upload_kwargs(env), stream=io.BytesIO(_PDF))
    # 修改 Provider 当前能力配置，历史记录的快照必须保持不变
    async with env["sessionmaker"]() as session:
        await SourceProviderRepository(session).upsert(
            _provider("sse", capabilities=["company_announcement", "issuer_ir"])
        )
        await session.commit()
    fetched = await service.get_source(created.record.source_id)
    assert fetched.provider_capabilities_snapshot == [
        "company_announcement",
        "document_download",
    ]
    # 新登记使用修改后的能力列表
    second = await service.ingest_upload(
        **_upload_kwargs(env, source_url="https://www.sse.com.cn/2024/000002.pdf"),
        stream=io.BytesIO(_PDF_OTHER),
    )
    assert second.record.provider_capabilities_snapshot == [
        "company_announcement",
        "issuer_ir",
    ]


@pytest.mark.asyncio
async def test_non_array_capabilities_snapshot_rejected(ingest_env) -> None:
    env = ingest_env
    artifact = await _create_artifact(env["sessionmaker"], env["raw_store"], _PDF)
    async with env["sessionmaker"]() as session:
        repo = SourceRecordRepository(session)
        with pytest.raises(IntegrityError):
            await repo.create(
                _record(
                    env["company_id"],
                    artifact.artifact_id,
                    provider_capabilities_snapshot={"company_announcement": True},
                )
            )
        await session.rollback()


@pytest.mark.asyncio
async def test_macro_only_provider_cannot_import_company_documents(ingest_env) -> None:
    # FRED 等宏观数据 Provider 仅具备 macro_data，不能用于公司文件导入
    env = ingest_env
    async with env["sessionmaker"]() as session:
        await SourceProviderRepository(session).upsert(
            _provider(
                "fred",
                provider_type="government_data",
                homepage_url="https://fred.stlouisfed.org",
                allowed_domains=["fred.stlouisfed.org"],
                capabilities=["macro_data"],
                acquisition_methods=["official_api"],
                requires_api_key=True,
            )
        )
        await session.commit()
    service = _service(env)
    with pytest.raises(SourceCapabilityNotAllowed):
        await service.ingest_upload(
            **_upload_kwargs(env, provider_key="fred"), stream=io.BytesIO(_PDF)
        )


# ------------------------------------------------------- network isolation


@pytest.mark.asyncio
async def test_real_http_transport_is_blocked(ingest_env) -> None:
    env = ingest_env
    # 显式注入真实 fetcher（无 MockTransport）：autouse guard 必须阻止真实外网
    service = _service(env, fetcher=SafePdfFetcher())
    with pytest.raises(AssertionError, match="real external HTTP is forbidden in tests"):
        await service.ingest_url(**_upload_kwargs(env))


@pytest.mark.asyncio
async def test_url_import_without_injected_fetcher_fails_fast(ingest_env) -> None:
    env = ingest_env
    # _service 默认 fetcher 是永不让步的 MockTransport：忘注入立即失败，
    # 不会回退到真实网络（guard 也一并阻止）。
    service = _service(env)
    with pytest.raises(AssertionError, match="must inject a MockTransport"):
        await service.ingest_url(**_upload_kwargs(env))
