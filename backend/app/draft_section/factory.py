"""LLM factory (stage 5B): Settings → DraftSectionModel。

- `deepseek` → `DeepSeekDraftSectionModel`（官方 langchain-deepseek
  integration）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.core.config import Settings
from app.draft_section.adapters import DeepSeekDraftSectionModel
from app.draft_section.model import DraftSectionModel
from app.llm.contracts import LLM_PROVIDER_DEEPSEEK
from app.llm.errors import UnsupportedLLMProviderError
from app.llm.instrumentation import LlmUsageObserver


def create_draft_section_model(
    settings: Settings, usage_observer: LlmUsageObserver | None = None
) -> DraftSectionModel:
    """根据 Settings.llm_provider 构造 DraftSectionModel（可选注入 usage_observer）。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekDraftSectionModel(settings, usage_observer=usage_observer)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")
