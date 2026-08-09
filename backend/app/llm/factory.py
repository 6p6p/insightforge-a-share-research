"""LLM factory (stage 3C.2.1): Settings → EvidenceExtractionModel。

- `deepseek` → `DeepSeekEvidenceExtractionModel`（官方 langchain-deepseek
  integration；**不用** ChatOpenAI + base_url 模拟 DeepSeek）；
- 未知 provider → `UnsupportedLLMProviderError`；
- 无 key 时**仍允许构造**（应用启动不调用工厂；调用时才由 provider 层报错）。
"""

from app.core.config import Settings
from app.evidence.extractor.adapters import DeepSeekEvidenceExtractionModel
from app.evidence.extractor.contracts import EvidenceExtractionModel
from app.llm.contracts import LLM_PROVIDER_DEEPSEEK
from app.llm.errors import MissingLLMCredentialsError, UnsupportedLLMProviderError


def create_evidence_extraction_model(settings: Settings) -> EvidenceExtractionModel:
    """根据 Settings.llm_provider 构造 EvidenceExtractionModel。"""
    provider = (settings.llm_provider or "").strip().lower()
    if provider == LLM_PROVIDER_DEEPSEEK:
        return DeepSeekEvidenceExtractionModel(settings)
    raise UnsupportedLLMProviderError(f"unsupported llm_provider: {provider or '<empty>'}")


def has_llm_credentials(settings: Settings) -> bool:
    """真实 smoke 的凭证预检：当前 provider 是否配置了 secret。"""
    if settings.deepseek_api_key is None:
        return False
    return bool(settings.deepseek_api_key.get_secret_value())


def require_llm_credentials(settings: Settings) -> None:
    """无凭证时抛 MissingLLMCredentialsError（smoke 显式失败而非误报成功）。"""
    if not has_llm_credentials(settings):
        raise MissingLLMCredentialsError("llm credentials are not configured")
