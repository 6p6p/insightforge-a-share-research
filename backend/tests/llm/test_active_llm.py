"""v1.2.8：统一 LLM runtime factory（get_active_llm）行为测试。

覆盖：
- deepseek provider → ChatDeepSeek（thinking disabled、model、temperature=0）；
- openai-compatible（openai/openrouter/custom/unknown）→ ChatOpenAI + base_url；
- Settings 空 provider → 走 openai-compatible（或明确错误）；
- has_llm_credentials / require_llm_credentials 对 deepseek / 通用 key 分派；
- active_config.apply_active_override 对 deepseek 与 openai-compatible 都覆盖 settings。
"""

from __future__ import annotations

import sys
from types import ModuleType
from typing import Any

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.llm.base import get_active_llm, get_active_llm_info, has_llm_credentials
from app.llm.errors import MissingLLMCredentialsError, UnsupportedLLMProviderError


def _settings(**overrides: Any) -> Settings:
    values = dict(
        _env_file=None,
        app_env="test",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
        llm_timeout_seconds=60,
        llm_max_retries=1,
    )
    values.update(overrides)
    return Settings(**values)


class _FakeChatDeepSeek:
    """记录构造参数的 ChatDeepSeek 替身。"""

    _recorded: dict | None = None
    _cls: type | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeChatDeepSeek._cls = type(self)
        _FakeChatDeepSeek._recorded = {"args": args, "kwargs": kwargs}

    @property
    def model(self) -> str:
        return self.kwargs["model"]

    @property
    def extra_body(self) -> dict | None:
        return self.kwargs.get("extra_body")


class _FakeChatOpenAI:
    _recorded: dict | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.kwargs = kwargs
        _FakeChatOpenAI._recorded = {"args": args, "kwargs": kwargs}

    @property
    def model(self) -> str:
        return self.kwargs.get("model")

    @property
    def openai_api_base(self) -> str | None:
        return self.kwargs.get("base_url")


@pytest.fixture(autouse=True)
def _fake_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 sys.modules 里的两个 SDK 换成记录构造参数的替身。

    get_active_llm 内部 `from langchain_deepseek import ChatDeepSeek` 是函数内
    lazy import，从 sys.modules 解析；覆盖 sys.modules 即可截获（真实 SDK 在
    测试环境已安装，但本文件的每个用例都命中替身）。
    """

    def deepseek_mod() -> ModuleType:
        mod = ModuleType("fake_langchain_deepseek")
        mod.ChatDeepSeek = _FakeChatDeepSeek
        return mod

    def openai_mod() -> ModuleType:
        mod = ModuleType("fake_langchain_openai")
        mod.ChatOpenAI = _FakeChatOpenAI
        return mod

    monkeypatch.setitem(sys.modules, "langchain_deepseek", deepseek_mod())
    monkeypatch.setitem(sys.modules, "langchain_openai", openai_mod())


# ----------------------------------------------------------------------
# get_active_llm：deepseek
# ----------------------------------------------------------------------


def test_get_active_llm_deepseek_constructs_chat_deepseek() -> None:
    s = _settings(
        deepseek_api_key=SecretStr("sk-deepseek"),
        llm_model="deepseek-v4-flash",
    )
    llm = get_active_llm(s)
    assert isinstance(llm, _FakeChatDeepSeek)
    assert llm.model == "deepseek-v4-flash"
    assert llm.extra_body == {"thinking": {"type": "disabled"}}


def test_get_active_llm_deepseek_no_thinking_param_when_base_url() -> None:
    s = _settings(
        deepseek_api_key=SecretStr("sk-deepseek"),
        llm_base_url="https://gateway.example/v1",
    )
    llm = get_active_llm(s)
    assert isinstance(llm, _FakeChatDeepSeek)
    assert llm.kwargs["base_url"] == "https://gateway.example/v1"
    assert llm.kwargs["extra_body"] == {"thinking": {"type": "disabled"}}


# ----------------------------------------------------------------------
# get_active_llm：openai-compatible
# ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider",
    ["openai", "openrouter", "custom", "siliconflow", "oneapi", "vllm", "unknown"],
)
def test_get_active_llm_any_openai_compatible_provider(provider: str) -> None:
    s = _settings(
        llm_provider=provider,
        llm_model="gpt-4o",
        llm_api_key=SecretStr("sk-openai"),
        llm_base_url="https://api.openai.com/v1",
    )
    llm = get_active_llm(s)
    assert isinstance(llm, _FakeChatOpenAI)
    assert llm.model == "gpt-4o"
    assert llm.openai_api_base == "https://api.openai.com/v1"


def test_get_active_llm_openai_without_base_url_ok() -> None:
    s = _settings(
        llm_provider="openai",
        llm_model="gpt-4o",
        llm_api_key=SecretStr("sk-openai"),
    )
    llm = get_active_llm(s)
    assert isinstance(llm, _FakeChatOpenAI)
    assert llm.openai_api_base is None


# ----------------------------------------------------------------------
# get_active_llm_info（前端「当前运行模型」展示；不含 key）
# ----------------------------------------------------------------------


def test_get_active_llm_info_deepseek() -> None:
    s = _settings(deepseek_api_key=SecretStr("sk-x"))
    info = get_active_llm_info(s)
    assert info["provider"] == "deepseek"
    assert info["model_id"] == "deepseek-v4-flash"
    assert info["has_api_key"] is True


def test_get_active_llm_info_openai() -> None:
    s = _settings(
        llm_provider="openrouter",
        llm_model="anthropic/claude-sonnet-4",
        llm_api_key=SecretStr("sk-y"),
        llm_base_url="https://openrouter.ai/api/v1",
    )
    info = get_active_llm_info(s)
    assert info["provider"] == "openrouter"
    assert info["model_id"] == "anthropic/claude-sonnet-4"
    assert info["base_url"] == "https://openrouter.ai/api/v1"
    assert info["has_api_key"] is True


def test_get_active_llm_info_no_key() -> None:
    assert get_active_llm_info(_settings())["has_api_key"] is False


# ----------------------------------------------------------------------
# 凭证 helper
# ----------------------------------------------------------------------


def test_has_llm_credentials_deepseek() -> None:
    assert has_llm_credentials(_settings(deepseek_api_key=SecretStr("sk-x"))) is True
    assert has_llm_credentials(_settings()) is False


def test_has_llm_credentials_openai_uses_llm_api_key() -> None:
    assert (
        has_llm_credentials(
            _settings(llm_provider="openai", llm_api_key=SecretStr("sk-y"))
        )
        is True
    )
    assert (
        has_llm_credentials(
            _settings(llm_provider="openai", deepseek_api_key=SecretStr("sk-deep"))
        )
        is False
    )


def test_require_llm_credentials_raises_when_missing() -> None:
    from app.llm.base import require_llm_credentials

    with pytest.raises(MissingLLMCredentialsError):
        require_llm_credentials(_settings())


# ----------------------------------------------------------------------
# factory 空 provider 语义
# ----------------------------------------------------------------------


def test_factory_empty_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """空 provider 在 factory 层显式失败（配置错误早失败）；非空任意 provider 放行。"""
    from app.analysis.macro.factory import create_macro_analysis_model

    with pytest.raises(UnsupportedLLMProviderError):
        create_macro_analysis_model(_settings(llm_provider=""))

    model = create_macro_analysis_model(_settings(llm_provider="openai"))
    assert model is not None
    assert model.model_id == "openai:deepseek-v4-flash"


# ----------------------------------------------------------------------
# active_config.apply_active_override
# ----------------------------------------------------------------------


class _FakeActive:
    provider = ""
    model_id = ""
    base_url = ""
    encrypted_api_key = None

    def __init__(self, provider, model_id, base_url, encrypted_api_key=None):
        self.provider = provider
        self.model_id = model_id
        self.base_url = base_url
        self.encrypted_api_key = encrypted_api_key


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("deepseek", "https://api.deepseek.com/v1"),
        ("openai", "https://api.openai.com/v1"),
        ("custom", "https://gateway.example/v1"),
    ],
)
def test_apply_active_override_applies_any_provider(monkeypatch, provider, base_url):
    from app.llm.active_config import apply_active_override

    class _FakeStore:
        def decrypt(self, enc):
            return "sk-decrypted"

    monkeypatch.setattr("app.llm.active_config.LlmConfigKeyStore", lambda s: _FakeStore())
    s = _settings()
    applied = apply_active_override(s, _FakeActive(provider, "my-model-9", base_url, "enc:abc"))
    assert applied is True
    assert s.llm_provider == provider
    assert s.llm_model == "my-model-9"
    assert s.llm_base_url == base_url
    if provider == "deepseek":
        assert s.deepseek_api_key is not None
        assert s.deepseek_api_key.get_secret_value() == "sk-decrypted"
    else:
        assert s.llm_api_key is not None
        assert s.llm_api_key.get_secret_value() == "sk-decrypted"


def test_apply_active_override_none_returns_false() -> None:
    from app.llm.active_config import apply_active_override

    assert apply_active_override(_settings(), None) is False
