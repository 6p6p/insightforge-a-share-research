"""LLM factory (stage 5E.2A): Settings → RevisionWriterModel。

- `deepseek` → `DeepSeekRevisionWriterModel`（官方 langchain-deepseek
  integration）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.core.config import Settings
from app.llm.errors import UnsupportedLLMProviderError
from app.llm.instrumentation import LlmUsageObserver
from app.revision.adapters import DeepSeekRevisionWriterModel
from app.revision.model import RevisionWriterModel


def create_revision_writer_model(
    settings: Settings, usage_observer: LlmUsageObserver | None = None
) -> RevisionWriterModel:
    """根据 Settings.llm_provider 构造 RevisionWriterModel（可选注入 usage_observer）。"""
    provider = (settings.llm_provider or "").strip().lower()
    if not provider:
        raise UnsupportedLLMProviderError("llm_provider is not configured")

    return DeepSeekRevisionWriterModel(settings, usage_observer=usage_observer)
