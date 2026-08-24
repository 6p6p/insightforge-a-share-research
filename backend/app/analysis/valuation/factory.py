"""LLM factory (stage 4C.2B.2): Settings → ValuationAnalysisModel。

- `deepseek` → `DeepSeekValuationAnalysisModel`（官方 langchain-deepseek
  integration）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.analysis.valuation.adapters import DeepSeekValuationAnalysisModel
from app.analysis.valuation.model import ValuationAnalysisModel
from app.core.config import Settings
from app.llm.errors import UnsupportedLLMProviderError
from app.llm.instrumentation import LlmUsageObserver


def create_valuation_analysis_model(
    settings: Settings, usage_observer: LlmUsageObserver | None = None
) -> ValuationAnalysisModel:
    """根据 Settings.llm_provider 构造 ValuationAnalysisModel（可选注入 usage_observer）。"""
    provider = (settings.llm_provider or "").strip().lower()
    if not provider:
        raise UnsupportedLLMProviderError("llm_provider is not configured")

    return DeepSeekValuationAnalysisModel(settings, usage_observer=usage_observer)
