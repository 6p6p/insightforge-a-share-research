"""API protocol tests for URL → provider auto resolution (V1.1 closure).

用 fake registry service 隔离协议层；真实解析逻辑由集成测试覆盖。
"""

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import get_source_registry_service
from app.services.source_registry_service import ResolvedProvider


class FakeRegistryService:
    def __init__(self) -> None:
        self.resolve_error: Exception | None = None
        self.captured: dict = {}

    async def resolve_provider_for_url(self, company_id, url):
        from app.core.errors import SourceUrlNotAllowed

        self.captured = {"company_id": company_id, "url": url}
        if self.resolve_error is not None:
            raise self.resolve_error
        if not url.startswith("https://"):
            raise SourceUrlNotAllowed()
        return ResolvedProvider(
            provider_key="issuer_official",
            display_name="上市公司官方网站",
            authority_tier=2,
            critical_claim_eligible=True,
            matched_by="issuer_domain",
        )


@pytest.fixture
def fake_registry() -> FakeRegistryService:
    return FakeRegistryService()


@pytest.fixture
def registry_client(app, fake_registry):
    app.dependency_overrides[get_source_registry_service] = lambda: fake_registry
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def test_resolve_provider_returns_resolved(registry_client, fake_registry) -> None:
    company_id = str(uuid4())
    response = registry_client.post(
        "/api/v1/source-providers/resolve",
        json={"company_id": company_id, "url": "https://www.catl.com"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider_key"] == "issuer_official"
    assert body["matched_by"] == "issuer_domain"
    assert body["authority_tier"] == 2
    assert body["critical_claim_eligible"] is True
    assert str(fake_registry.captured["company_id"]) == company_id
    assert fake_registry.captured["url"] == "https://www.catl.com"


def test_resolve_provider_error_envelope(registry_client, fake_registry) -> None:
    from app.core.errors import SourceUrlNotAllowed

    fake_registry.resolve_error = SourceUrlNotAllowed()
    response = registry_client.post(
        "/api/v1/source-providers/resolve",
        json={"company_id": str(uuid4()), "url": "https://evil.example.net"},
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "source_url_not_allowed"


def test_resolve_provider_rejects_bad_url(registry_client, fake_registry) -> None:
    response = registry_client.post(
        "/api/v1/source-providers/resolve",
        json={"company_id": str(uuid4()), "url": "http://not-https.com/x"},
    )
    assert response.status_code == 400
    assert fake_registry.captured["url"] == "http://not-https.com/x"
