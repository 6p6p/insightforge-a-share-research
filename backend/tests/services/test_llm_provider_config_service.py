"""Unit tests for LlmProviderConfigService (v1.2.7-B)."""

from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from cryptography.fernet import Fernet

from app.db.models.llm_provider_config import LlmProviderConfigModel
from app.schemas.llm_provider_config import (
    LlmConfigCreateRequest,
    LlmConfigTestRequest,
    LlmConfigUpdateRequest,
)
from app.services.llm_provider_config_service import (
    LlmConfigKeyStore,
    LlmProviderConfigNotFound,
    LlmProviderConfigService,
)


class _FakeKeyStore(LlmConfigKeyStore):
    def __init__(self) -> None:
        self._fernet = Fernet(Fernet.generate_key())

    def encrypt(self, plaintext):
        if plaintext is None or not plaintext.strip():
            return None
        return self._fernet.encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token):
        if not token:
            return None
        try:
            return self._fernet.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            return None


class _FakeRepo:
    def __init__(self) -> None:
        self.rows = []

    async def create(self, config):
        config.id = config.id or uuid4()
        now = datetime.now(UTC)
        config.created_at = config.created_at or now
        config.updated_at = config.updated_at or now
        self.rows.append(config)
        return config

    async def get_by_id(self, config_id):
        for r in self.rows:
            if r.id == config_id:
                return r
        return None

    async def list_all(self):
        return list(self.rows)

    async def clear_active(self):
        for r in self.rows:
            r.is_active = False

    async def delete(self, config):
        self.rows = [r for r in self.rows if r.id != config.id]

    async def flush(self):
        # v1.2.8：service 经 repository.flush()（修复 .session 500）。
        pass

    @property
    def session(self):
        class _S:
            async def flush(self):
                pass

        return _S()


def _service(repo, key_store=None):
    return LlmProviderConfigService(repo, object(), key_store=key_store or _FakeKeyStore())


def _row(**overrides):
    now = datetime.now(UTC)
    defaults = dict(
        id=uuid4(),
        provider="deepseek",
        display_name="DeepSeek",
        model_id="deepseek-v4-flash",
        base_url="https://api.deepseek.com/v1",
        encrypted_api_key=None,
        is_active=False,
        has_api_key=False,
        created_at=now,
        updated_at=now,
    )
    defaults.update(overrides)
    return LlmProviderConfigModel(**defaults)


@pytest.mark.asyncio
async def test_create_stores_encrypted_key_and_never_plain():
    repo = _FakeRepo()
    service = _service(repo)
    created = await service.create(
        LlmConfigCreateRequest(
            provider="deepseek",
            display_name="DeepSeek",
            model_id="deepseek-v4-flash",
            base_url="https://api.deepseek.com/v1",
            api_key="sk-plain-secret",
            is_active=True,
        )
    )
    assert created.has_api_key is True
    assert created.is_active is True
    row = repo.rows[0]
    assert row.encrypted_api_key != "sk-plain-secret"
    assert row.encrypted_api_key
    # 明文绝不进响应
    assert "sk-plain-secret" not in created.model_dump_json()


@pytest.mark.asyncio
async def test_create_url_encrypts_identity():
    repo = _FakeRepo()
    key_store = _FakeKeyStore()
    service = _service(repo, key_store)
    await service.create(
        LlmConfigCreateRequest(
            provider="deepseek",
            display_name="DeepSeek",
            model_id="m",
            api_key="k1",
        )
    )
    plain = key_store.decrypt(repo.rows[0].encrypted_api_key)
    assert plain == "k1"


@pytest.mark.asyncio
async def test_update_changes_fields_and_key():
    repo = _FakeRepo()
    service = _service(repo)
    row = _row()
    repo.rows.append(row)
    updated = await service.update(
        row.id,
        LlmConfigUpdateRequest(
            display_name="新版",
            model_id="deepseek-v4",
            api_key="new-key",
            is_active=True,
        ),
    )
    assert updated.display_name == "新版"
    assert updated.model_id == "deepseek-v4"
    assert updated.has_api_key is True
    assert row.is_active is True


@pytest.mark.asyncio
async def test_set_active_clears_others():
    repo = _FakeRepo()
    service = _service(repo)
    a = _row(display_name="A")
    b = _row(display_name="B")
    a.is_active = True
    repo.rows.extend([a, b])
    result = await service.set_active(b.id, True)
    assert result.is_active is True
    assert a.is_active is False
    assert b.is_active is True


@pytest.mark.asyncio
async def test_delete_removes_row():
    repo = _FakeRepo()
    service = _service(repo)
    row = _row()
    repo.rows.append(row)
    await service.delete(row.id)
    assert repo.rows == []


@pytest.mark.asyncio
async def test_delete_missing_raises():
    repo = _FakeRepo()
    service = _service(repo)
    with pytest.raises(LlmProviderConfigNotFound):
        await service.delete(uuid4())


@pytest.mark.asyncio
async def test_test_connection_success_and_failure():
    repo = _FakeRepo()
    service = _service(repo)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"id": "x"})
        return httpx.Response(404)

    service._transport = httpx.MockTransport(handler)
    ok = await service.test_connection(
        LlmConfigTestRequest(
            provider="openai",
            model_id="gpt-4o-mini",
            base_url="https://api.openai.com/v1",
            api_key="sk-test",
        )
    )
    assert ok.ok is True
    assert ok.latency_ms is not None

    def deny_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "invalid api key"}})

    service._transport = httpx.MockTransport(deny_handler)
    from app.services.llm_provider_config_service import LlmConfigTestFailed

    with pytest.raises(LlmConfigTestFailed):
        await service.test_connection(
            LlmConfigTestRequest(
                provider="openai",
                model_id="gpt-4o-mini",
                base_url="https://api.openai.com/v1",
                api_key="bad",
            )
        )


# ----------------------------------------------------------------------
# v1.2.8：update key 保留语义 + test_connection 读目标配置自己的 key
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_without_api_key_keeps_existing_key():
    repo = _FakeRepo()
    key_store = _FakeKeyStore()
    service = _service(repo, key_store)
    row = _row(encrypted_api_key=key_store.encrypt("sk-original"), has_api_key=True)
    repo.rows.append(row)

    updated = await service.update(
        row.id,
        LlmConfigUpdateRequest(display_name="改名", model_id="deepseek-v4", is_active=True),
    )
    assert updated.has_api_key is True
    assert key_store.decrypt(row.encrypted_api_key) == "sk-original"


@pytest.mark.asyncio
async def test_update_with_empty_api_key_keeps_existing_key():
    repo = _FakeRepo()
    key_store = _FakeKeyStore()
    service = _service(repo, key_store)
    row = _row(encrypted_api_key=key_store.encrypt("sk-original"), has_api_key=True)
    repo.rows.append(row)

    await service.update(row.id, LlmConfigUpdateRequest(api_key="   "))
    assert key_store.decrypt(row.encrypted_api_key) == "sk-original"


@pytest.mark.asyncio
async def test_test_connection_uses_target_config_key_by_id():
    # 目标配置自己的 encrypt key，而不是“当前 active 配置”的 key。
    repo = _FakeRepo()
    key_store = _FakeKeyStore()
    service = _service(repo, key_store)
    active_other = _row(
        display_name="ActiveOther",
        provider="openai",
        model_id="gpt-4o-mini",
        base_url="https://api.openai.com/v1",
        encrypted_api_key=key_store.encrypt("sk-other-active"),
        has_api_key=True,
        is_active=True,
    )
    target = _row(
        display_name="Target",
        encrypted_api_key=key_store.encrypt("sk-target"),
        has_api_key=True,
    )
    repo.rows.extend([active_other, target])

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={"id": "x"})

    service._transport = httpx.MockTransport(handler)
    ok = await service.test_connection(
        LlmConfigTestRequest(provider="deepseek", model_id="deepseek-v4-flash", api_key=None),
        config_id=target.id,
    )
    assert ok.ok is True
    # 用的是目标配置的 key，而不是 active 配置的 key
    assert seen["auth"] == "Bearer sk-target"


@pytest.mark.asyncio
async def test_test_connection_decrypt_failure_raises_business_error():
    repo = _FakeRepo()
    service = _service(repo)
    row = _row(encrypted_api_key="garbage-not-valid", has_api_key=True)
    repo.rows.append(row)
    from app.services.llm_provider_config_service import LlmConfigTestFailed

    with pytest.raises(LlmConfigTestFailed):
        await service.test_connection(
            LlmConfigTestRequest(provider="deepseek", model_id="deepseek-v4-flash", api_key=None),
            config_id=row.id,
        )
