"""API tests for /llm-configs endpoints (v1.2.7-B)."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from app.api.dependencies import (
    get_langgraph_checkpoint_manager,
    get_llm_provider_config_service,
)
from app.db.dependencies import get_database
from app.main import create_app
from app.schemas.llm_provider_config import (
    LlmConfigCreateRequest,
    LlmConfigResponse,
    LlmConfigTestResponse,
    LlmConfigUpdateRequest,
)
from app.services.llm_provider_config_service import (
    LlmProviderConfigNotFound,
)
from app.vectorstore.dependencies import get_chroma


def _response(**overrides) -> LlmConfigResponse:
    defaults = dict(
        id=uuid4(),
        provider="deepseek",
        display_name="DeepSeek",
        model_id="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        has_api_key=True,
        is_active=False,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    defaults.update(overrides)
    return LlmConfigResponse(**defaults)


class FakeLlmConfigService:
    def __init__(self) -> None:
        self.items: list[LlmConfigResponse] = []
        self.create_error: Exception | None = None
        self.test_result: LlmConfigTestResponse | None = None
        self.deleted: list[UUID] = []
        self.last_test_config_id: UUID | None = None

    async def list(self):
        return list(self.items)

    async def get(self, config_id: UUID):
        for item in self.items:
            if item.id == config_id:
                return item
        raise LlmProviderConfigNotFound()

    async def create(self, request: LlmConfigCreateRequest):
        if self.create_error is not None:
            raise self.create_error
        item = _response(
            provider=request.provider,
            display_name=request.display_name,
            model_id=request.model_id,
            base_url=request.base_url,
            has_api_key=bool(request.api_key and request.api_key.strip()),
            is_active=request.is_active,
        )
        self.items.append(item)
        return item

    async def update(self, config_id: UUID, request: LlmConfigUpdateRequest):
        item = await self.get(config_id)
        if request.display_name is not None:
            item.display_name = request.display_name
        if request.model_id is not None:
            item.model_id = request.model_id
        return item

    async def set_active(self, config_id: UUID, active: bool):
        item = await self.get(config_id)
        item.is_active = active
        return item

    async def delete(self, config_id: UUID):
        item = await self.get(config_id)
        self.deleted.append(config_id)
        self.items.remove(item)

    async def test_connection(self, request, config_id=None):
        if self.test_result is not None:
            return self.test_result
        # 记录调用（供 /{id}/test 断言 config_id 已透传）
        self.last_test_config_id = config_id
        return LlmConfigTestResponse(ok=True, latency_ms=12, message="连接成功")


@pytest.fixture
def fake_service() -> FakeLlmConfigService:
    return FakeLlmConfigService()


@pytest.fixture
def app(test_settings, fake_database, fake_chroma, fake_langgraph, fake_service):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    application.dependency_overrides[get_langgraph_checkpoint_manager] = lambda: fake_langgraph
    application.dependency_overrides[get_llm_provider_config_service] = lambda: fake_service
    return application


def test_list_configs(client, fake_service):
    fake_service.items = [_response(), _response(display_name="OpenAI", provider="openai")]
    response = client.get("/api/v1/llm-configs")
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert "items" in body


def test_create_returns_201_without_plain_key(client, fake_service):
    response = client.post(
        "/api/v1/llm-configs",
        json={
            "provider": "openai",
            "display_name": "OpenAI",
            "model_id": "gpt-4o-mini",
            "base_url": "https://api.openai.com/v1",
            "api_key": "sk-secret",
            "is_active": False,
        },
    )
    assert response.status_code == 201
    body = response.json()
    assert body["has_api_key"] is True
    assert "api_key" not in body
    assert "sk-secret" not in response.text


def test_update_config(client, fake_service):
    item = _response()
    fake_service.items = [item]
    response = client.put(
        f"/api/v1/llm-configs/{item.id}",
        json={"display_name": "DeepSeek 新版", "model_id": "deepseek-v4"},
    )
    assert response.status_code == 200
    assert response.json()["display_name"] == "DeepSeek 新版"


def test_delete_returns_204(client, fake_service):
    item = _response()
    fake_service.items = [item]
    response = client.delete(f"/api/v1/llm-configs/{item.id}")
    assert response.status_code == 204
    assert fake_service.deleted == [item.id]


def test_delete_missing_returns_404(client, fake_service):
    item = _response()
    fake_service.items = [item]
    response = client.delete(f"/api/v1/llm-configs/{uuid4()}")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "llm_config_not_found"


def test_set_active(client, fake_service):
    item = _response()
    fake_service.items = [item]
    response = client.post(f"/api/v1/llm-configs/{item.id}/active", json={"is_active": True})
    assert response.status_code == 200
    assert response.json()["is_active"] is True


def test_test_connection(client, fake_service):
    fake_service.test_result = LlmConfigTestResponse(ok=True, latency_ms=8, message="连接成功")
    response = client.post(
        "/api/v1/llm-configs/test",
        json={
            "provider": "custom",
            "model_id": "my-model",
            "api_key": "k",
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_test_stored_config_passes_own_id(client, fake_service):
    # v1.2.8 修复：/{id}/test 必须把目标 config_id 透传给 service（读取该配置
    # 自己的加密 key），而不是误用“当前 active 配置”的 key。
    item = _response()
    fake_service.items = [item]
    response = client.post(f"/api/v1/llm-configs/{item.id}/test")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert fake_service.last_test_config_id == item.id
