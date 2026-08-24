"""LLM provider config API schemas (v1.2.7-B).

API key 只从 Create/Update/Test 请求进入，response 永不返回明文 key（仅
`has_api_key` 标记）。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class LlmConfigCreateRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    display_name: str = Field(min_length=1, max_length=120)
    model_id: str = Field(min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    is_active: bool = False


class LlmConfigUpdateRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=120)
    provider: str | None = Field(default=None, min_length=1, max_length=32)
    model_id: str | None = Field(default=None, min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None = Field(default=None, max_length=512)
    is_active: bool | None = None


class LlmConfigSetActiveRequest(BaseModel):
    is_active: bool


class LlmConfigTestRequest(BaseModel):
    provider: str = Field(min_length=1, max_length=32)
    model_id: str = Field(min_length=1, max_length=160)
    base_url: str | None = Field(default=None, max_length=255)
    api_key: str | None
    # 复用已存配置（API key 未明文回传）时传 True，service 用加密 key 尝试。
    use_stored_key: bool = False


class LlmConfigResponse(BaseModel):
    id: UUID
    provider: str
    display_name: str
    model_id: str
    base_url: str | None
    has_api_key: bool
    is_active: bool
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class LlmConfigListResponse(BaseModel):
    items: list[LlmConfigResponse]
    total: int
    active_id: UUID | None = None


class LlmConfigTestResponse(BaseModel):
    ok: bool
    latency_ms: int | None = None
    message: str
