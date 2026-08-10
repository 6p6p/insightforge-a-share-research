"""LLM factory (stage 4C.1B): Settings → MacroAnalysisModel。

- `deepseek` → `DeepSeekMacroAnalysisModel`（官方 langchain-deepseek
  integration）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.analysis.macro.adapters import DeepSeekMacroAnalysisModel
from app.analysis.macro.model import MacroAnalysisModel
from app.core.config import Settings
from app.llm.contracts import LLM_PROVIDER_DEEPSEEK
from app.llm.errors import UnsupportedLLMProviderError


def create_macro_analysis_model(settings: Settings) -> MacroAnalysisModel:
    """根据 Settings.llm_provider 构造 MacroAnalysisModel。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekMacroAnalysisModel(settings)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")
