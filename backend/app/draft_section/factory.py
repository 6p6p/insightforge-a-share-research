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


def create_draft_section_model(settings: Settings) -> DraftSectionModel:
    """根据 Settings.llm_provider 构造 DraftSectionModel。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekDraftSectionModel(settings)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")
