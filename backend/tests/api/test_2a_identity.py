"""Tests for company identity and source registry API."""

from uuid import UUID, uuid4

import pytest

from app.api.dependencies import (
    get_company_identity_service,
    get_langgraph_checkpoint_manager,
    get_source_registry_service,
)
from app.core.errors import (
    CompanyIdentityAmbiguous,
    CompanyIdentityNotFound,
    InvalidCompanyQuery,
    SourceProviderNotFound,
)
from app.db.dependencies import get_database
from app.domain.companies import CompanyMatchType
from app.main import create_app
from app.schemas.company import CompanyIdentityResponse, CompanyResolutionResponse
from app.schemas.source_provider import SourceProviderResponse
from app.vectorstore.dependencies import get_chroma


def _company_response(**overrides: object) -> CompanyIdentityResponse:
    defaults: dict = {
        "company_id": uuid4(),
        "exchange": "SSE",
        "security_code": "600519",
        "identity_key": "SSE:600519",
        "board": "sse_main",
        "official_name": "贵州茅台酒股份有限公司",
        "short_name": "贵州茅台",
        "listing_status": "listed",
        "listing_date": None,
        "delisting_date": None,
        "identity_source_provider_key": "sse",
        "identity_source_url": "https://www.sse.com.cn",
        "source_updated_at": None,
    }
    defaults.update(overrides)
    return CompanyIdentityResponse.model_validate(defaults)


def _provider_response(**overrides: object) -> SourceProviderResponse:
    defaults: dict = {
        "provider_key": "sse",
        "display_name": "上海证券交易所",
        "provider_type": "exchange",
        "authority_tier": 1,
        "homepage_url": "https://www.sse.com.cn",
        "allowed_domains": ["sse.com.cn"],
        "capabilities": ["company_announcement", "document_download"],
        "acquisition_methods": ["official_web_page", "official_file_download"],
        "exchange_scope": ["SSE"],
        "requires_api_key": False,
        "critical_claim_eligible": True,
        "enabled": True,
    }
    defaults.update(overrides)
    return SourceProviderResponse.model_validate(defaults)


class FakeCompanyService:
    def __init__(self) -> None:
        self.resolve_error: Exception | None = None
        self.resolve_result: CompanyResolutionResponse | None = None
        self.company_error: Exception | None = None

    async def resolve(self, query: str) -> CompanyResolutionResponse:
        if self.resolve_error is not None:
            raise self.resolve_error
        if self.resolve_result is not None:
            return self.resolve_result
        return CompanyResolutionResponse(
            company=_company_response(),
            match_type=CompanyMatchType.SECURITY_CODE,
            matched_value=query,
        )

    async def get_company(self, company_id: UUID) -> CompanyIdentityResponse:
        if self.company_error is not None:
            raise self.company_error
        return _company_response(company_id=company_id)


class FakeRegistryService:
    def __init__(self) -> None:
        self.providers: list[SourceProviderResponse] = []
        self.detail: SourceProviderResponse | None = None
        self.detail_error: Exception | None = None
        self.last_filters: dict = {}

    async def list_providers(self, **filters) -> list:
        self.last_filters = filters
        return self.providers

    async def get_provider(self, provider_key: str) -> SourceProviderResponse:
        if self.detail_error is not None:
            raise self.detail_error
        if self.detail is not None:
            return self.detail
        return _provider_response(provider_key=provider_key)


@pytest.fixture
def fake_company_service() -> FakeCompanyService:
    return FakeCompanyService()


@pytest.fixture
def fake_registry_service() -> FakeRegistryService:
    return FakeRegistryService()


@pytest.fixture
def app(
    test_settings,
    fake_database,
    fake_chroma,
    fake_langgraph,
    fake_company_service,
    fake_registry_service,
):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    application.dependency_overrides[get_langgraph_checkpoint_manager] = lambda: fake_langgraph
    application.dependency_overrides[get_company_identity_service] = lambda: fake_company_service
    application.dependency_overrides[get_source_registry_service] = lambda: fake_registry_service
    return application


def test_resolve_company_200(client, fake_company_service) -> None:
    fake_company_service.resolve_result = CompanyResolutionResponse(
        company=_company_response(),
        match_type=CompanyMatchType.IDENTITY_KEY,
        matched_value="SSE:600519",
    )
    response = client.post("/api/v1/companies/resolve", json={"query": "SSE:600519"})
    assert response.status_code == 200
    assert response.json()["match_type"] == "identity_key"


def test_resolve_explicit_symbol_200(client, fake_company_service) -> None:
    fake_company_service.resolve_result = CompanyResolutionResponse(
        company=_company_response(),
        match_type=CompanyMatchType.EXPLICIT_SYMBOL,
        matched_value="600519.sh",
    )
    response = client.post("/api/v1/companies/resolve", json={"query": "600519.SH"})
    assert response.status_code == 200
    body = response.json()
    assert body["match_type"] == "explicit_symbol"
    assert body["matched_value"] == "600519.sh"
    assert body["company"]["identity_key"] == "SSE:600519"


def test_resolve_invalid_400(client, fake_company_service) -> None:
    fake_company_service.resolve_error = InvalidCompanyQuery()
    response = client.post("/api/v1/companies/resolve", json={"query": "FOO:123"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_company_query"


def test_resolve_not_found_404(client, fake_company_service) -> None:
    fake_company_service.resolve_error = CompanyIdentityNotFound()
    response = client.post("/api/v1/companies/resolve", json={"query": "不存在公司"})
    assert response.status_code == 404
    assert response.json()["error"]["request_id"]


def test_resolve_ambiguous_409(client, fake_company_service) -> None:
    fake_company_service.resolve_error = CompanyIdentityAmbiguous()
    response = client.post("/api/v1/companies/resolve", json={"query": "600519"})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "company_identity_ambiguous"


def test_get_company_200(client, fake_company_service) -> None:
    response = client.get(f"/api/v1/companies/{uuid4()}")
    assert response.status_code == 200
    assert response.json()["exchange"] == "SSE"


def test_get_company_404(client, fake_company_service) -> None:
    fake_company_service.company_error = CompanyIdentityNotFound()
    response = client.get(f"/api/v1/companies/{uuid4()}")
    assert response.status_code == 404


def test_list_providers_200(client, fake_registry_service) -> None:
    fake_registry_service.providers = [
        _provider_response(),
        _provider_response(provider_key="fred", display_name="FRED"),
    ]
    response = client.get("/api/v1/source-providers")
    assert response.status_code == 200
    assert response.json()["total"] == 2


def test_list_providers_filters(client, fake_registry_service) -> None:
    client.get(
        "/api/v1/source-providers", params={"capability": "regulation", "enabled_only": "false"}
    )
    assert fake_registry_service.last_filters["capability"].value == "regulation"
    assert fake_registry_service.last_filters["enabled_only"] is False


def test_get_provider_detail_200(client, fake_registry_service) -> None:
    fake_registry_service.detail = _provider_response(provider_key="csrc")
    response = client.get("/api/v1/source-providers/csrc")
    assert response.status_code == 200
    assert response.json()["provider_key"] == "csrc"


def test_get_provider_404(client, fake_registry_service) -> None:
    fake_registry_service.detail_error = SourceProviderNotFound()
    response = client.get("/api/v1/source-providers/unknown")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "source_provider_not_found"


def test_openapi_contains_2a_endpoints(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/companies/resolve" in schema["paths"]
    assert "/api/v1/companies/{company_id}" in schema["paths"]
    assert "/api/v1/source-providers" in schema["paths"]
    assert "/api/v1/source-providers/{provider_key}" in schema["paths"]
