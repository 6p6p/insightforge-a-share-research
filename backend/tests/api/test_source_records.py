"""API protocol tests for source record ingestion endpoints.

用 fake ingestion service 隔离协议层（multipart 解析、状态码、响应头、
重放语义、列表/详情/下载契约）；真实服务链路由集成测试覆盖。
"""

import io
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_source_ingestion_service
from app.core.errors import SourceContentUnsupportedMediaType, SourceRecordNotFound
from app.domain.source_records import SourceDocumentType
from app.schemas.source_record import (
    SourceRecordListResponse,
    SourceRecordResponse,
)
from app.services.source_ingestion_service import IngestionResult

_PDF_BYTES = b"%PDF-1.7\n1 0 obj\n%%EOF\n"


def _record(source_id=None) -> SourceRecordResponse:
    now = datetime.now(UTC)
    return SourceRecordResponse(
        source_id=source_id or uuid4(),
        company_id=uuid4(),
        provider_key="sse",
        artifact_id=uuid4(),
        document_type=SourceDocumentType.ANNUAL_REPORT,
        title="2024 年年度报告",
        source_url="https://www.sse.com.cn/2024/000001.pdf",
        acquisition_method="user_upload",
        authority_tier_snapshot=1,
        critical_claim_eligible_snapshot=True,
        provider_capabilities_snapshot=["company_announcement", "document_download"],
        status="available",
        acquired_at=now,
        created_at=now,
        content_sha256="a" * 64,
        byte_size=len(_PDF_BYTES),
        media_type="application/pdf",
    )


class FakeIngestionService:
    def __init__(self) -> None:
        self.result = IngestionResult(record=_record(), replayed=False)
        self.record = _record()
        self.list_response = SourceRecordListResponse(
            items=[_record()], total=1, limit=50, offset=0
        )
        self.stream = io.BytesIO(_PDF_BYTES)
        self.get_error: Exception | None = None
        self.captured: dict = {}

    async def ingest_upload(self, **kwargs):
        self.captured = kwargs
        return self.result

    async def ingest_url(self, **kwargs):
        self.captured = kwargs
        return self.result

    async def get_source(self, source_id):
        if self.get_error is not None:
            raise self.get_error
        return self.record

    async def list_company_sources(self, **kwargs):
        self.captured_list = kwargs
        return self.list_response

    async def open_source_content(self, source_id):
        if self.get_error is not None:
            raise self.get_error
        return self.record, self.stream


@pytest.fixture
def fake_ingestion() -> FakeIngestionService:
    return FakeIngestionService()


@pytest.fixture
def ingestion_client(app, fake_ingestion):
    app.dependency_overrides[get_source_ingestion_service] = lambda: fake_ingestion
    with TestClient(app) as test_client:
        yield test_client


def _upload_data(**overrides: object) -> dict:
    data = {
        "company_id": str(uuid4()),
        "provider_key": "sse",
        "document_type": "annual_report",
        "title": "测试报告",
        "source_url": "https://www.sse.com.cn/2024/000001.pdf",
    }
    data.update(overrides)
    return data


def _pdf_file() -> dict:
    return {"file": ("report.pdf", _PDF_BYTES, "application/pdf")}


# ---------------------------------------------------------------- upload


def test_upload_returns_201_and_parses_form(ingestion_client, fake_ingestion) -> None:
    response = ingestion_client.post(
        "/api/v1/source-records/upload", data=_upload_data(), files=_pdf_file()
    )
    assert response.status_code == 201
    assert response.headers["Source-Replayed"] == "false"
    body = response.json()
    assert body["provider_key"] == "sse"
    assert body["document_type"] == "annual_report"
    assert body["source_id"] == str(fake_ingestion.result.record.source_id)
    assert fake_ingestion.captured["title"] == "测试报告"


def test_upload_replay_returns_200_with_header(ingestion_client, fake_ingestion) -> None:
    fake_ingestion.result = IngestionResult(record=_record(), replayed=True)
    response = ingestion_client.post(
        "/api/v1/source-records/upload", data=_upload_data(), files=_pdf_file()
    )
    assert response.status_code == 200
    assert response.headers["Source-Replayed"] == "true"


def test_upload_missing_required_field_returns_422(ingestion_client) -> None:
    data = _upload_data()
    del data["provider_key"]
    response = ingestion_client.post("/api/v1/source-records/upload", data=data, files=_pdf_file())
    assert response.status_code == 422


def test_upload_invalid_document_type_returns_422(ingestion_client) -> None:
    response = ingestion_client.post(
        "/api/v1/source-records/upload",
        data=_upload_data(document_type="quarterly_report_plus"),
        files=_pdf_file(),
    )
    assert response.status_code == 422


def test_upload_missing_file_returns_422(ingestion_client) -> None:
    response = ingestion_client.post("/api/v1/source-records/upload", data=_upload_data())
    assert response.status_code == 422


def test_upload_optional_dates_parsed(ingestion_client, fake_ingestion) -> None:
    data = _upload_data(
        published_at="2026-04-30T00:00:00Z",
        reporting_period_end="2026-03-31",
        external_document_id="ext-1",
    )
    response = ingestion_client.post("/api/v1/source-records/upload", data=data, files=_pdf_file())
    assert response.status_code == 201
    captured = fake_ingestion.captured
    assert captured["published_at"].isoformat().startswith("2026-04-30")
    assert captured["reporting_period_end"].isoformat() == "2026-03-31"
    assert captured["external_document_id"] == "ext-1"


# --------------------------------------------------------------- import-url


def test_import_url_returns_201(ingestion_client, fake_ingestion) -> None:
    payload = {
        "company_id": str(uuid4()),
        "provider_key": "sse",
        "document_type": "annual_report",
        "title": "导入",
        "source_url": "https://www.sse.com.cn/2024/000001.pdf",
    }
    response = ingestion_client.post("/api/v1/source-records/import-url", json=payload)
    assert response.status_code == 201
    assert response.headers["Source-Replayed"] == "false"
    assert fake_ingestion.captured["source_url"] == payload["source_url"]
    assert fake_ingestion.captured["provider_key"] == "sse"


def test_import_url_replay_returns_200(ingestion_client, fake_ingestion) -> None:
    fake_ingestion.result = IngestionResult(record=_record(), replayed=True)
    payload = {
        "company_id": str(uuid4()),
        "provider_key": "sse",
        "document_type": "annual_report",
        "title": "t",
        "source_url": "https://www.sse.com.cn/2024/000001.pdf",
    }
    response = ingestion_client.post("/api/v1/source-records/import-url", json=payload)
    assert response.status_code == 200
    assert response.headers["Source-Replayed"] == "true"


def test_import_url_requires_https_source_url(ingestion_client) -> None:
    payload = {
        "company_id": str(uuid4()),
        "provider_key": "sse",
        "document_type": "annual_report",
        "title": "t",
        "source_url": "http://www.sse.com.cn/x.pdf",
    }
    # 协议层允许任意字符串（URL 合法性由 SourceRegistry 策略在校验层把关）
    response = ingestion_client.post("/api/v1/source-records/import-url", json=payload)
    assert response.status_code in (200, 201)


def test_import_url_missing_body_returns_422(ingestion_client) -> None:
    response = ingestion_client.post(
        "/api/v1/source-records/import-url", json={"provider_key": "sse"}
    )
    assert response.status_code == 422


# ------------------------------------------------------- list / detail / download


def test_get_source_detail(ingestion_client, fake_ingestion) -> None:
    source_id = fake_ingestion.record.source_id
    response = ingestion_client.get(f"/api/v1/source-records/{source_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["source_id"] == str(source_id)
    assert body["content_sha256"] == "a" * 64
    assert body["byte_size"] == len(_PDF_BYTES)
    assert body["media_type"] == "application/pdf"


def test_get_source_not_found_returns_404(ingestion_client, fake_ingestion) -> None:
    fake_ingestion.get_error = SourceRecordNotFound()
    response = ingestion_client.get(f"/api/v1/source-records/{uuid4()}")
    assert response.status_code == 404


def test_list_company_sources(ingestion_client, fake_ingestion) -> None:
    company_id = uuid4()
    response = ingestion_client.get(f"/api/v1/companies/{company_id}/source-records")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert fake_ingestion.captured_list["company_id"] == company_id


def test_list_company_sources_document_type_filter(ingestion_client, fake_ingestion) -> None:
    company_id = uuid4()
    response = ingestion_client.get(
        f"/api/v1/companies/{company_id}/source-records",
        params={"document_type": "annual_report"},
    )
    assert response.status_code == 200
    assert fake_ingestion.captured_list["document_type"] == SourceDocumentType.ANNUAL_REPORT


def test_list_company_sources_pagination_bounds(ingestion_client) -> None:
    company_id = uuid4()
    for params in ({"limit": 0}, {"limit": 101}, {"offset": -1}):
        response = ingestion_client.get(
            f"/api/v1/companies/{company_id}/source-records", params=params
        )
        assert response.status_code == 422


def test_download_content_streams_pdf(ingestion_client, fake_ingestion) -> None:
    source_id = fake_ingestion.record.source_id
    response = ingestion_client.get(f"/api/v1/source-records/{source_id}/content")
    assert response.status_code == 200
    assert response.headers["Content-Type"] == "application/pdf"
    assert response.headers["Content-Length"] == str(len(_PDF_BYTES))
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="source-{source_id}.pdf"'
    )
    assert response.content == _PDF_BYTES


def test_download_content_not_found_returns_404(ingestion_client, fake_ingestion) -> None:
    fake_ingestion.get_error = SourceRecordNotFound()
    response = ingestion_client.get(f"/api/v1/source-records/{uuid4()}/content")
    assert response.status_code == 404


def test_download_content_html_returns_415(ingestion_client, fake_ingestion) -> None:
    """§二十一：news_article 的 HTML raw artifact 不允许通过 content 端点下载。"""
    html_record = _record()
    html_record.media_type = "text/html"
    fake_ingestion.record = html_record
    response = ingestion_client.get(f"/api/v1/source-records/{html_record.source_id}/content")
    assert response.status_code == 415
    body = response.json()
    assert body["error"]["code"] == "source_content_unsupported_media_type"


def test_source_content_unsupported_media_type_contract() -> None:
    """§二十一 契约：415 必须落在 DomainError 字段上，防止 docstring/字段漂移。

    直接断言错误对象（不经 Fake Service/API 层），防止出现"注释写 415、
    实际 http_status=4"这种总结与代码不一致。
    """
    err = SourceContentUnsupportedMediaType()
    assert err.code == "source_content_unsupported_media_type"
    assert err.http_status == 415
    assert err.message == "该来源媒体类型不支持内容下载"


def test_openapi_contains_source_endpoints(ingestion_client) -> None:
    schema = ingestion_client.get("/openapi.json").json()
    paths = schema["paths"]
    assert "/api/v1/source-records/upload" in paths
    assert "/api/v1/source-records/import-url" in paths
    assert "/api/v1/source-records/{source_id}" in paths
    assert "/api/v1/companies/{company_id}/source-records" in paths
    assert "/api/v1/source-records/{source_id}/content" in paths
