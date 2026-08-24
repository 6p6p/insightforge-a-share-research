"""Business logic for user-configured LLM provider configs (v1.2.7-B).

- CRUD + 设置 active + 测试连接（OpenAI-Compatible /chat/completions）。
- API key 以 Fernet 密文存储；response 永不返回明文。
- 优先级由 runtime 解析（数据库 active 配置 > 环境变量 > 默认）。
"""

import base64
import hashlib
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
from cryptography.fernet import Fernet

from app.core.config import Settings
from app.core.errors import DomainError
from app.db.models.llm_provider_config import LlmProviderConfigModel
from app.repositories.llm_provider_config_repository import LlmProviderConfigRepository
from app.schemas.llm_provider_config import (
    LlmConfigCreateRequest,
    LlmConfigResponse,
    LlmConfigTestRequest,
    LlmConfigTestResponse,
    LlmConfigUpdateRequest,
)


class LlmProviderConfigNotFound(DomainError):
    code = "llm_config_not_found"
    http_status = 404
    message = "模型配置不存在"


class LlmConfigTestFailed(DomainError):
    code = "llm_config_test_failed"
    http_status = 400
    message = "模型配置验证失败"

    def __init__(self, detail: str | None = None) -> None:
        if detail:
            self.message = f"模型配置验证失败：{detail}"


_DEFAULT_CHAT_SUFFIX = "/chat/completions"
_TIMEOUT_SECONDS = 20.0


def _default_base_url(provider: str) -> str | None:
    provider = (provider or "").strip().lower()
    if provider == "deepseek":
        return "https://api.deepseek.com/v1"
    if provider == "openai":
        return "https://api.openai.com/v1"
    if provider == "openrouter":
        return "https://openrouter.ai/api/v1"
    return None


class LlmConfigKeyStore:
    """Fernet key 管理：优先 settings.llm_config_encryption_key，其次持久化 key 文件。"""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _derive_fernet_key(self, seed: bytes) -> bytes:
        return base64.urlsafe_b64encode(hashlib.sha256(seed).digest())

    def _fetch_raw(self) -> bytes:
        configured = self._settings.llm_config_encryption_key
        if configured is not None:
            raw = str(configured).strip()
            if raw:
                return raw.encode("utf-8")
        key_path: Path = self._settings.llm_config_key_path
        try:
            key_path.parent.mkdir(parents=True, exist_ok=True)
            if key_path.exists():
                return key_path.read_bytes()
            generated = base64.urlsafe_b64encode(os.urandom(32))
            key_path.write_bytes(generated)
            return generated
        except OSError:
            return base64.urlsafe_b64encode(os.urandom(32))

    def fernet(self) -> Fernet:
        raw = self._fetch_raw()
        fernet_key = raw if len(raw) >= 43 else self._derive_fernet_key(raw)
        return Fernet(fernet_key)

    def encrypt(self, plaintext: str | None) -> str | None:
        if plaintext is None or not plaintext.strip():
            return None
        return self.fernet().encrypt(plaintext.encode("utf-8")).decode("utf-8")

    def decrypt(self, token: str | None) -> str | None:
        if not token:
            return None
        try:
            return self.fernet().decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception:
            return None


def _extract_error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
        err = data.get("error") if isinstance(data, dict) else None
        if isinstance(err, dict) and err.get("message"):
            return str(err["message"])
        if isinstance(data, dict) and data.get("message"):
            return str(data["message"])
    except Exception:
        pass
    if response.text:
        return response.text[:160]
    return f"HTTP {response.status_code}"


class LlmProviderConfigService:
    def __init__(
        self,
        repository: LlmProviderConfigRepository,
        settings: Settings,
        key_store: LlmConfigKeyStore | None = None,
        httpx_timeout: float = _TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._key_store = key_store or LlmConfigKeyStore(settings)
        self._timeout = httpx_timeout
        self._transport = transport

    @staticmethod
    def _to_response(config: LlmProviderConfigModel) -> LlmConfigResponse:
        return LlmConfigResponse(
            id=config.id,
            provider=config.provider,
            display_name=config.display_name,
            model_id=config.model_id,
            base_url=config.base_url,
            has_api_key=bool(config.encrypted_api_key),
            is_active=config.is_active,
            created_at=config.created_at,
            updated_at=config.updated_at,
        )

    async def create(self, request: LlmConfigCreateRequest) -> LlmConfigResponse:
        if request.is_active:
            await self._repository.clear_active()
        config = LlmProviderConfigModel(
            provider=request.provider.strip(),
            display_name=request.display_name.strip(),
            model_id=request.model_id.strip(),
            base_url=request.base_url.strip() if request.base_url else None,
            encrypted_api_key=self._key_store.encrypt(request.api_key),
            is_active=request.is_active,
            has_api_key=bool(request.api_key and request.api_key.strip()),
        )
        await self._repository.create(config)
        return self._to_response(config)

    async def list(self) -> list[LlmConfigResponse]:
        rows = await self._repository.list_all()
        return [self._to_response(r) for r in rows]

    async def get(self, config_id: uuid.UUID) -> LlmConfigResponse:
        row = await self._repository.get_by_id(config_id)
        if row is None:
            raise LlmProviderConfigNotFound()
        return self._to_response(row)

    async def update(
        self,
        config_id: uuid.UUID,
        request: LlmConfigUpdateRequest,
    ) -> LlmConfigResponse:
        row = await self._repository.get_by_id(config_id)
        if row is None:
            raise LlmProviderConfigNotFound()
        if request.is_active is True:
            await self._repository.clear_active()
        if request.display_name is not None:
            row.display_name = request.display_name.strip()
        if request.provider is not None:
            row.provider = request.provider.strip()
        if request.model_id is not None:
            row.model_id = request.model_id.strip()
        if request.base_url is not None:
            row.base_url = request.base_url.strip() or None
        if request.api_key is not None and request.api_key.strip():
            # v1.2.8 修复：空 / None key 不覆盖已有 encrypted_api_key（避免编辑保存后丢 key）。
            row.encrypted_api_key = self._key_store.encrypt(request.api_key)
            row.has_api_key = True
        if request.is_active is not None:
            row.is_active = request.is_active
        row.updated_at = datetime.now(UTC)
        await self._repository.flush()
        return self._to_response(row)

    async def set_active(self, config_id: uuid.UUID, active: bool) -> LlmConfigResponse:
        row = await self._repository.get_by_id(config_id)
        if row is None:
            raise LlmProviderConfigNotFound()
        if active:
            await self._repository.clear_active()
            row.is_active = True
        else:
            row.is_active = False
        row.updated_at = datetime.now(UTC)
        await self._repository.flush()
        return self._to_response(row)

    async def delete(self, config_id: uuid.UUID) -> None:
        row = await self._repository.get_by_id(config_id)
        if row is None:
            raise LlmProviderConfigNotFound()
        await self._repository.delete(row)

    async def test_connection(
        self,
        request: LlmConfigTestRequest,
        config_id: uuid.UUID | None = None,
    ) -> LlmConfigTestResponse:
        base_url = (request.base_url or "").strip() or _default_base_url(request.provider)
        api_key = (request.api_key or "").strip()

        # v1.2.8 修复：优先读目标配置（config_id）自己的加密 key，而不是误读
        # 当前 active 行（测试非 active 配置时拿错 key）。config_id 缺失时才回退
        # 到「测试当前 active」语义。
        if not api_key and config_id is not None:
            stored = await self._repository.get_by_id(config_id)
            if stored is None:
                raise LlmProviderConfigNotFound()
            if not stored.encrypted_api_key:
                raise LlmConfigTestFailed(detail="该配置未保存 API Key")
            decrypted = self._key_store.decrypt(stored.encrypted_api_key)
            if not decrypted:
                raise LlmConfigTestFailed(detail="API Key 解密失败，请重新保存")
            api_key = decrypted
            # 目标配置自己的 provider / model / base_url 才是测试对象（覆盖 request 中
            # route 未提供的字段，且保证与 DB 一致）。
            base_url = (
                (stored.base_url or "").strip() or _default_base_url(stored.provider)
            ) or base_url
            model_id = stored.model_id
        else:
            model_id = request.model_id
            if not api_key and getattr(request, "use_stored_key", False):
                async for _row_any in self._iter_active_rows():
                    decrypted = self._key_store.decrypt(_row_any.encrypted_api_key)
                    if decrypted:
                        api_key = decrypted
                        break

        if not base_url:
            return LlmConfigTestResponse(ok=False, message="未提供 Base URL")
        if not api_key:
            return LlmConfigTestResponse(ok=False, message="未提供 API Key")
        endpoint = base_url.rstrip("/") + _DEFAULT_CHAT_SUFFIX
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if not model_id:
            return LlmConfigTestResponse(ok=False, message="未提供 Model ID")
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": "ping"}],
            "max_tokens": 1,
        }
        started = time.monotonic()
        try:
            client_kwargs = dict(timeout=self._timeout, transport=self._transport)
            async with httpx.AsyncClient(**client_kwargs) as client:
                response = await client.post(endpoint, headers=headers, json=payload)
            latency = int((time.monotonic() - started) * 1000)
        except httpx.HTTPError:
            raise LlmConfigTestFailed() from None
        if response.status_code in (200, 201):
            return LlmConfigTestResponse(ok=True, latency_ms=latency, message="连接成功")
        _detail = _extract_error_detail(response)
        raise LlmConfigTestFailed(detail=_detail) from None

    async def _iter_active_rows(self):
        rows = await self._repository.list_all()
        for row in rows:
            if row.is_active:
                yield row
