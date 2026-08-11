"""LLM factory (stage 5E.2A): Settings → RevisionWriterModel。

- `deepseek` → `DeepSeekRevisionWriterModel`（官方 langchain-deepseek
  integration）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.core.config import Settings
from app.llm.contracts import LLM_PROVIDER_DEEPSEEK
from app.llm.errors import UnsupportedLLMProviderError
from app.revision.adapters import DeepSeekRevisionWriterModel
from app.revision.model import RevisionWriterModel


def create_revision_writer_model(settings: Settings) -> RevisionWriterModel:
    """根据 Settings.llm_provider 构造 RevisionWriterModel。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekRevisionWriterModel(settings)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")
