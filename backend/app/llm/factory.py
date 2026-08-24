"""LLM factory (stage 3C.2.1): Settings → EvidenceExtractionModel。

- 任意受支持 provider → wrapper（`get_active_llm` 在 adapter 内部按
  settings.llm_provider / llm_base_url 分派 deepseek 或 openai-compatible）；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.core.config import Settings
from app.evidence.extractor.adapters import DeepSeekEvidenceExtractionModel
from app.evidence.extractor.contracts import EvidenceExtractionModel
from app.llm.errors import MissingLLMCredentialsError, UnsupportedLLMProviderError
from app.llm.instrumentation import LlmUsageObserver


def create_evidence_extraction_model(
    settings: Settings, usage_observer: LlmUsageObserver | None = None
) -> EvidenceExtractionModel:
    """根据 Settings.llm_provider 构造 EvidenceExtractionModel。

    可选 `usage_observer` 注入 eval 层 collector（生产默认 None）。
    """
    provider = (settings.llm_provider or "").strip().lower()
    if not provider:
        raise UnsupportedLLMProviderError("llm_provider is not configured")

    return DeepSeekEvidenceExtractionModel(settings, usage_observer=usage_observer)


def has_llm_credentials(settings: Settings) -> bool:
    """真实 smoke 的凭证预检：当前 provider 是否配置了 secret（deepseek 或通用 key）。"""
    from app.llm.base import has_llm_credentials as _base_has

    return _base_has(settings)


def require_llm_credentials(settings: Settings) -> None:
    """无凭证时抛 MissingLLMCredentialsError（smoke 显式失败而非误报成功）。"""
    if not has_llm_credentials(settings):
        raise MissingLLMCredentialsError("llm credentials are not configured")
