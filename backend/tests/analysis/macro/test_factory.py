"""Macro analysis model factory unit tests (stage 4C.1B)。

验证：
- llm_provider=deepseek → DeepSeekMacroAnalysisModel（model_id =
  `{provider}:{model}`，如 deepseek:deepseek-v4-flash）；
- 未知 provider → UnsupportedLLMProviderError；
- 构造 adapter 不触发 langchain 导入（lazy import，仅 analyze() 时加载）。
"""

import pytest

from app.analysis.macro.adapters import DeepSeekMacroAnalysisModel
from app.analysis.macro.factory import create_macro_analysis_model
from app.core.config import Settings
from app.llm.errors import UnsupportedLLMProviderError


def _settings(**overrides) -> Settings:
    values = dict(
        _env_file=None,
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
        llm_provider="deepseek",
        llm_model="deepseek-v4-flash",
    )
    values.update(overrides)
    return Settings(**values)


def test_factory_returns_deepseek_adapter_for_deepseek_provider() -> None:
    settings = _settings(llm_provider="deepseek")
    model = create_macro_analysis_model(settings)
    assert isinstance(model, DeepSeekMacroAnalysisModel)
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_factory_model_id_reflects_provider_and_model() -> None:
    settings = _settings(llm_provider="deepseek", llm_model="deepseek-v4-flash")
    assert create_macro_analysis_model(settings).model_id == "deepseek:deepseek-v4-flash"


def test_factory_rejects_unknown_provider() -> None:
    settings = _settings(llm_provider="openai")
    with pytest.raises(UnsupportedLLMProviderError):
        create_macro_analysis_model(settings)


def test_factory_rejects_empty_provider() -> None:
    settings = _settings(llm_provider="")
    with pytest.raises(UnsupportedLLMProviderError):
        create_macro_analysis_model(settings)
