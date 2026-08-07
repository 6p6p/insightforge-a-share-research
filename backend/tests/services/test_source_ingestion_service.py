"""Tests for SourceIngestionService validation and orchestration.

_persist 与数据库写路径由集成测试覆盖；本文件用 fake session + mock 仓库
验证校验分支与上传/URL 导入编排。
"""

import hashlib
import io
from uuid import uuid4

import httpx
import pytest
from sqlalchemy.sql.selectable import Select

from app.acquisition.http_fetcher import SafePdfFetcher
from app.core.errors import (
    CompanyIdentityNotFound,
    InvalidPdfFile,
    NewsArticleIngestionNotAllowed,
    RawArtifactNotFound,
    SourceCapabilityNotAllowed,
    SourceProviderDisabled,
    SourceProviderNotFound,
    SourceRecordNotFound,
    SourceUrlNotAllowed,
)
from app.db.models.company import CompanyModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.domain.source_records import SourceDocumentType
from app.repositories.company_repository import CompanyRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.source_ingestion_service import SourceIngestionService
from app.storage.raw_store import StoredRawArtifact

_PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
_ALLOWED_DOMAIN = "www.sse.com.cn"
_GOOD_URL = f"https://{_ALLOWED_DOMAIN}/2024/000001.pdf"


def _provider(**overrides: object) -> SourceProviderModel:
    defaults: dict = {
        "provider_key": "sse",
        "display_name": "上海证券交易所",
        "provider_type": "exchange",
        "authority_tier": 1,
        "homepage_url": "https://www.sse.com.cn",
        "allowed_domains": [_ALLOWED_DOMAIN],
        "capabilities": ["company_announcement", "document_download"],
        "acquisition_methods": ["official_web_page"],
        "exchange_scope": ["SSE"],
        "requires_api_key": False,
        "critical_claim_eligible": True,
        "enabled": True,
    }
    defaults.update(overrides)
    return SourceProviderModel(**defaults)


def _company() -> CompanyModel:
    return CompanyModel(
        company_id=uuid4(),
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


class FakeResult:
    def __init__(self, rows: list | None) -> None:
        self._rows = rows if rows is not None else []

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None

    def scalar_one(self):
        return self._rows[0]

    def scalars(self):
        return self

    def all(self):
        return self._rows


class FakeSession:
    """execute 按查询目标模型分发；标量/计数查询返回 count_value。"""

    def __init__(self, rows_by_model: dict | None = None, count_value: int = 0) -> None:
        self._rows_by_model = rows_by_model or {}
        self._count_value = count_value
        self.committed = False
        self.rolled_back = False

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False

    async def execute(self, stmt):
        if isinstance(stmt, Select):
            model = stmt.column_descriptions[0]["type"]
            if model in self._rows_by_model:
                return FakeResult(self._rows_by_model[model])
            if isinstance(model, type) and hasattr(model, "__tablename__"):
                # 已知 ORM 模型但未注入行 → 空结果（区别于标量/计数查询）
                return FakeResult([])
            return FakeResult([self._count_value])
        raise NotImplementedError(f"unhandled statement: {stmt!r}")

    def add(self, obj) -> None:
        self._added = obj

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True


class FakeRawStore:
    def __init__(self) -> None:
        self.put_calls: list[bytes] = []

    def put_pdf_stream(self, stream: io.BytesIO) -> StoredRawArtifact:
        data = stream.read()
        self.put_calls.append(data)
        digest = hashlib.sha256(data).hexdigest()
        return StoredRawArtifact(
            content_sha256=digest,
            storage_key=f"sha256/{digest[:2]}/{digest[2:4]}/{digest}.pdf",
            byte_size=len(data),
            media_type="application/pdf",
            newly_created=True,
        )


class RejectingRawStore:
    def put_pdf_stream(self, stream) -> StoredRawArtifact:
        raise InvalidPdfFile()


def _forbidden_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError("URL import test must inject a MockTransport; refusing real network")


def _make_service(
    sessionmaker=None,
    raw_store=None,
    fetcher: SafePdfFetcher | None = None,
) -> SourceIngestionService:
    # 默认 fetcher 是永不让步的 MockTransport：URL 导入忘记注入 MockTransport
    # 会立即失败，而不是回退到真实 fetcher。
    if fetcher is None:
        fetcher = SafePdfFetcher(transport=httpx.MockTransport(_forbidden_handler))
    return SourceIngestionService(
        sessionmaker=sessionmaker or (lambda: FakeSession()),
        raw_store=raw_store or FakeRawStore(),
        fetcher=fetcher,
        max_bytes=1024 * 1024,
    )


async def _stub_load(monkeypatch, *, provider: SourceProviderModel | None = None, company=None):
    async def fake_company_get(self, company_id):
        return company

    async def fake_provider_get(self, provider_key):
        return provider

    monkeypatch.setattr(CompanyRepository, "get_by_id", fake_company_get)
    monkeypatch.setattr(SourceProviderRepository, "get_by_key", fake_provider_get)


@pytest.mark.asyncio
async def test_load_company_and_provider_company_not_found(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=None)
    service = _make_service()
    with pytest.raises(CompanyIdentityNotFound):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_load_company_and_provider_provider_not_found(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=None, company=_company())
    service = _make_service()
    with pytest.raises(SourceProviderNotFound):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="nope",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_load_company_and_provider_disabled(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(enabled=False), company=_company())
    service = _make_service()
    with pytest.raises(SourceProviderDisabled):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_load_company_and_provider_requires_download_capability(monkeypatch) -> None:
    await _stub_load(
        monkeypatch,
        provider=_provider(capabilities=["macro_data"]),
        company=_company(),
    )
    service = _make_service()
    with pytest.raises(SourceCapabilityNotAllowed):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_load_company_and_provider_url_not_allowed(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=_company())
    service = _make_service()
    with pytest.raises(SourceUrlNotAllowed):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url="https://evil.example.org/a.pdf",
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_ingest_upload_stores_stream_and_persists(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=_company())
    raw_store = FakeRawStore()
    service = _make_service(raw_store=raw_store)
    captured: dict = {}

    async def fake_persist(self, **kwargs):
        captured.update(kwargs)
        return "persisted"

    monkeypatch.setattr(SourceIngestionService, "_persist", fake_persist)

    result = await service.ingest_upload(
        company_id=uuid4(),
        provider_key="sse",
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title="2024 年年度报告",
        source_url=_GOOD_URL,
        published_at=None,
        reporting_period_end=None,
        external_document_id=None,
        stream=io.BytesIO(_PDF),
    )
    assert result == "persisted"
    assert raw_store.put_calls == [_PDF]
    assert captured["stored"].content_sha256 == hashlib.sha256(_PDF).hexdigest()
    assert captured["stored"].byte_size == len(_PDF)
    assert captured["stored"].media_type == "application/pdf"
    assert captured["acquisition_method"] == "user_upload"
    # 快照由真实 _persist 内部从 provider.capabilities 稳定排序写入（集成测试覆盖）；
    # 此处验证传入 _persist 的 provider 携带完整能力列表。
    assert captured["provider"].capabilities == ["company_announcement", "document_download"]


@pytest.mark.asyncio
async def test_ingest_upload_invalid_pdf_propagates(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=_company())
    service = _make_service(raw_store=RejectingRawStore())
    with pytest.raises(InvalidPdfFile):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(b"not a pdf"),
        )


@pytest.mark.asyncio
async def test_ingest_url_fetches_and_persists(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=_company())

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_PDF,
            headers={"content-type": "application/pdf", "content-length": str(len(_PDF))},
        )

    service = _make_service(
        raw_store=FakeRawStore(),
        fetcher=SafePdfFetcher(transport=httpx.MockTransport(handler)),
    )
    captured: dict = {}

    async def fake_persist(self, **kwargs):
        captured.update(kwargs)
        return "persisted"

    monkeypatch.setattr(SourceIngestionService, "_persist", fake_persist)

    result = await service.ingest_url(
        company_id=uuid4(),
        provider_key="sse",
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title="t",
        source_url=_GOOD_URL,
        published_at=None,
        reporting_period_end=None,
        external_document_id=None,
    )
    assert result == "persisted"
    assert captured["stored"].content_sha256 == hashlib.sha256(_PDF).hexdigest()
    assert captured["acquisition_method"] == "user_provided_url"


@pytest.mark.asyncio
async def test_ingest_url_rejects_disallowed_domain(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=_company())
    service = _make_service()
    with pytest.raises(SourceUrlNotAllowed):
        await service.ingest_url(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.ANNUAL_REPORT,
            title="t",
            source_url="https://evil.example.org/a.pdf",
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
        )


@pytest.mark.asyncio
async def test_ingest_upload_rejects_news_article(monkeypatch) -> None:
    """§二十：news_article 不能通过 upload 注入（守卫先于 provider 校验触发）。"""
    service = _make_service()
    with pytest.raises(NewsArticleIngestionNotAllowed):
        await service.ingest_upload(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.NEWS_ARTICLE,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
            stream=io.BytesIO(_PDF),
        )


@pytest.mark.asyncio
async def test_ingest_url_rejects_news_article(monkeypatch) -> None:
    """§二十：news_article 不能通过 import-url 注入。"""
    service = _make_service()
    with pytest.raises(NewsArticleIngestionNotAllowed):
        await service.ingest_url(
            company_id=uuid4(),
            provider_key="sse",
            document_type=SourceDocumentType.NEWS_ARTICLE,
            title="t",
            source_url=_GOOD_URL,
            published_at=None,
            reporting_period_end=None,
            external_document_id=None,
        )


@pytest.mark.asyncio
async def test_get_source_not_found() -> None:
    service = _make_service(sessionmaker=lambda: FakeSession())
    with pytest.raises(SourceRecordNotFound):
        await service.get_source(uuid4())


@pytest.mark.asyncio
async def test_get_source_missing_artifact() -> None:
    record = SourceRecordModel(
        source_id=uuid4(),
        company_id=uuid4(),
        provider_key="sse",
        artifact_id=uuid4(),
        document_type="annual_report",
        title="t",
        source_url=_GOOD_URL,
        acquisition_method="user_upload",
        status="available",
        authority_tier_snapshot=1,
        critical_claim_eligible_snapshot=True,
        provider_capabilities_snapshot=[],
    )
    session = FakeSession(rows_by_model={SourceRecordModel: [record]})
    service = _make_service(sessionmaker=lambda: session)
    with pytest.raises(RawArtifactNotFound):
        await service.get_source(record.source_id)


@pytest.mark.asyncio
async def test_list_company_sources_empty() -> None:
    session = FakeSession(rows_by_model={SourceRecordModel: []})
    service = _make_service(sessionmaker=lambda: session)
    result = await service.list_company_sources(uuid4(), None, limit=50, offset=0)
    assert result.items == []
    assert result.total == 0
    assert result.limit == 50
    assert result.offset == 0


@pytest.mark.asyncio
async def test_open_source_content_not_found() -> None:
    service = _make_service(sessionmaker=lambda: FakeSession())
    with pytest.raises(SourceRecordNotFound):
        await service.open_source_content(uuid4())


# ------------------------------------------------------- network isolation


@pytest.mark.asyncio
async def test_mock_transport_not_affected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=_PDF,
            headers={"content-type": "application/pdf", "content-length": str(len(_PDF))},
        )

    fetcher = SafePdfFetcher(transport=httpx.MockTransport(handler))
    pdf = await fetcher.fetch(_GOOD_URL, [_ALLOWED_DOMAIN], 1024 * 1024)
    try:
        assert pdf.content_stream.read() == _PDF
    finally:
        pdf.close()


@pytest.mark.asyncio
async def test_real_transport_blocked_by_guard() -> None:
    # 未注入 transport → 真实 httpx AsyncHTTPTransport → autouse guard 阻止
    fetcher = SafePdfFetcher()
    with pytest.raises(AssertionError, match="real external HTTP is forbidden in tests"):
        await fetcher.fetch(_GOOD_URL, [_ALLOWED_DOMAIN], 1024 * 1024)


@pytest.mark.asyncio
async def test_upload_never_touches_fetcher(monkeypatch) -> None:
    await _stub_load(monkeypatch, provider=_provider(), company=_company())
    service = _make_service(raw_store=FakeRawStore())

    async def boom(self, *args, **kwargs):
        raise AssertionError("fetcher must not be called for uploads")

    monkeypatch.setattr(SafePdfFetcher, "fetch", boom)

    async def fake_persist(self, **kwargs):
        return "persisted"

    monkeypatch.setattr(SourceIngestionService, "_persist", fake_persist)

    result = await service.ingest_upload(
        company_id=uuid4(),
        provider_key="sse",
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title="t",
        source_url=_GOOD_URL,
        published_at=None,
        reporting_period_end=None,
        external_document_id=None,
        stream=io.BytesIO(_PDF),
    )
    assert result == "persisted"
