"""LLM factory (stage 4D.1B): Settings → SynthesisAnalysisModel。

- `deepseek` → `DeepSeekSynthesisAnalysisModel`（官方 langchain-deepseek
  integration）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.analysis.synthesis.adapters import DeepSeekSynthesisAnalysisModel
from app.analysis.synthesis.model import SynthesisAnalysisModel
from app.core.config import Settings
from app.llm.contracts import LLM_PROVIDER_DEEPSEEK
from app.llm.errors import UnsupportedLLMProviderError


def create_synthesis_analysis_model(settings: Settings) -> SynthesisAnalysisModel:
    """根据 Settings.llm_provider 构造 SynthesisAnalysisModel。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekSynthesisAnalysisModel(settings)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")
