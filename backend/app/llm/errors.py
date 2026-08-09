"""LLM runtime errors (stage 3C.2.1).

错误消息**不含** API key / 完整 prompt / raw provider response / DB URL。
"""


class LLMError(Exception):
    """LLM 运行时稳定错误基类。"""


class UnsupportedLLMProviderError(LLMError):
    """Settings.llm_provider 不是受支持的 provider。"""


class MissingLLMCredentialsError(LLMError):
    """当前 provider 需要凭证但未配置（真实 smoke 的 pending_credentials 判定用）。"""
