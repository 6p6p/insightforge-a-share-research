"""Synthesis analysis model factory unit tests (stage 4D.1B).

验证：
- llm_provider=deepseek → DeepSeekSynthesisAnalysisModel（model_id =
  `{provider}:{model}`，如 deepseek:deepseek-v4-flash）；
- 未知 / 空 provider → UnsupportedLLMProviderError；
- 构造 adapter 不触发 langchain 导入（lazy import，仅 analyze() 时加载）。
"""

import pytest

from app.analysis.synthesis.adapters import DeepSeekSynthesisAnalysisModel
from app.analysis.synthesis.factory import create_synthesis_analysis_model
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
    model = create_synthesis_analysis_model(_settings(llm_provider="deepseek"))
    assert isinstance(model, DeepSeekSynthesisAnalysisModel)
    assert model.model_id == "deepseek:deepseek-v4-flash"


def test_factory_model_id_reflects_provider_and_model() -> None:
    assert (
        create_synthesis_analysis_model(
            _settings(llm_provider="deepseek", llm_model="deepseek-v4-flash")
        ).model_id
        == "deepseek:deepseek-v4-flash"
    )


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(UnsupportedLLMProviderError):
        create_synthesis_analysis_model(_settings(llm_provider="openai"))


def test_factory_rejects_empty_provider() -> None:
    with pytest.raises(UnsupportedLLMProviderError):
        create_synthesis_analysis_model(_settings(llm_provider=""))
