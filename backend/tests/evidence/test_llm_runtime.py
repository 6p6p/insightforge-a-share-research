"""LLM runtime 单元测试（stage 3C.2.1）：Settings 配置 + factory + DeepSeek adapter。

零网络、零真实 Key：adapter 构造与 model_id 不调用 provider；factory 分派只读
Settings。真实调用（extract）不在自动测试范围（conftest 禁止真实外网）。
"""

import pytest

from app.core.config import Settings
from app.evidence.extractor.adapters import DeepSeekEvidenceExtractionModel
from app.llm.factory import create_evidence_extraction_model, has_llm_credentials


def _settings(**overrides) -> Settings:
    base = dict(
        _env_file=None,
        app_env="test",
        log_level="DEBUG",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )
    base.update(overrides)
    return Settings(**base)


def test_default_llm_settings() -> None:
    s = _settings()
    assert s.llm_provider == "deepseek"
    assert s.llm_model == "deepseek-chat"
    assert s.llm_timeout_seconds == 60
    assert s.llm_max_retries == 1
    assert s.deepseek_api_key is None


def test_empty_deepseek_key_normalized_to_none() -> None:
    assert _settings(deepseek_api_key="").deepseek_api_key is None
    assert _settings(deepseek_api_key="   ").deepseek_api_key is None


def test_secret_value_and_no_leak_in_repr() -> None:
    s = _settings(deepseek_api_key="sk-test-secret")
    assert s.deepseek_api_key is not None
    assert s.deepseek_api_key.get_secret_value() == "sk-test-secret"
    assert "sk-test-secret" not in repr(s)


def test_invalid_llm_timeout_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(llm_timeout_seconds=0)
    with pytest.raises(ValueError):
        _settings(llm_timeout_seconds=61)


def test_invalid_llm_max_retries_rejected() -> None:
    with pytest.raises(ValueError):
        _settings(llm_max_retries=-1)
    with pytest.raises(ValueError):
        _settings(llm_max_retries=11)


def test_factory_returns_deepseek_adapter() -> None:
    model = create_evidence_extraction_model(_settings())
    assert isinstance(model, DeepSeekEvidenceExtractionModel)
    assert model.model_id == "deepseek:deepseek-chat"


def test_factory_unsupported_provider() -> None:
    with pytest.raises(Exception) as exc_info:
        create_evidence_extraction_model(_settings(llm_provider="openai"))
    from app.llm.errors import UnsupportedLLMProviderError

    assert isinstance(exc_info.value, UnsupportedLLMProviderError)


def test_adapter_has_no_tools_or_web_search() -> None:
    model = create_evidence_extraction_model(_settings())
    assert not hasattr(model, "tools")
    assert not hasattr(model, "web_search")


def test_adapter_constructs_without_credentials() -> None:
    # 无 key 仍允许构造（应用启动不调用 factory / extract）。
    model = DeepSeekEvidenceExtractionModel(_settings())
    assert model.model_id == "deepseek:deepseek-chat"


def test_has_llm_credentials() -> None:
    assert has_llm_credentials(_settings()) is False
    assert has_llm_credentials(_settings(deepseek_api_key="sk-x")) is True
