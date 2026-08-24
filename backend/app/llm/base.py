"""Unified LLM runtime factory (v1.2.8).

业务代码**禁止**直接实例化 `ChatDeepSeek` / `ChatOpenAI`；必须经
`get_active_llm(settings)` 获取 LangChain-compatible ChatModel。

按 settings.llm_provider 分派：
- provider == "deepseek" → ChatDeepSeek（官方 langchain-deepseek，显式关闭
  thinking：structured-output 需要稳定受约束输出）；
- 其他 provider（openai / openrouter / custom / siliconflow / oneapi / vllm）
  → ChatOpenAI（openai-compatible，base_url 决定 /chat/completions 端点，经
  base_url 覆盖 OpenAI / OpenRouter / SiliconFlow / OneAPI / vLLM 等任意兼容
  网关，不为每个平台写独立 provider）。

Key 读取：
- deepseek → settings.deepseek_api_key；
- 其他 → settings.llm_api_key（DB active 配置经 active_config 注入，或 .env）。

深度安全：
- API key 只经 langchain 构造参数传入，不落到日志 / error message；
- get_active_llm 只做构造（0 network）；调用失败由 provider 层（adapter）映射。
"""

from app.core.config import Settings
from app.llm.errors import UnsupportedLLMProviderError

# 所需的模型名参数
_DEEPSEEK_THINKING_DISABLED = {"thinking": {"type": "disabled"}}


def _resolve_token(secret) -> str | None:
    """SecretStr → 明文；None / 空 → None。"""
    if secret is None:
        return None
    value = getattr(secret, "get_secret_value", None)
    if value is None:
        return None
    plain = value()
    if not plain:
        return None
    return plain


def get_active_llm(settings: Settings, temperature: float = 0.0) -> object:
    """返回当前 active 的 LangChain-compatible ChatModel（构造 0 network）。

    经 settings.llm_provider / llm_model / llm_base_url / llm_api_key /
    deepseek_api_key 分派；由 lifespan 的 active_config 已把 DB 配置覆盖到
    settings（v1.2.7-B/v1.2.8），故本函数无需自己查 DB。
    """
    provider = (settings.llm_provider or "").strip().lower()
    model = settings.llm_model.strip()
    if not model:
        raise UnsupportedLLMProviderError("llm_model is not configured")

    common = {
        "model": model,
        "temperature": temperature,
        "timeout": settings.llm_timeout_seconds,
        "max_retries": settings.llm_max_retries,
    }

    if provider == "deepseek":
        from langchain_deepseek import ChatDeepSeek

        api_key = _resolve_token(settings.deepseek_api_key)
        kwargs: dict = {"api_key": api_key}
        base_url = (settings.llm_base_url or "").strip()
        if base_url:
            kwargs["base_url"] = base_url
        return ChatDeepSeek(
            **common,
            **kwargs,
            # structured-output 稳定性：显式关闭 thinking（DeepSeek V4 Flash
            # 默认开启 thinking，产生 reasoning_content，破坏受约束输出）。
            extra_body=_DEEPSEEK_THINKING_DISABLED,
        )

    # OpenAI-compatible
    from langchain_openai import ChatOpenAI

    api_key = _resolve_token(settings.llm_api_key)
    kwargs = {"api_key": api_key}
    base_url = (settings.llm_base_url or "").strip()
    if base_url:
        kwargs["base_url"] = base_url
    return ChatOpenAI(
        **common,
        **kwargs,
    )


def get_active_llm_info(settings: Settings) -> dict:
    """只读的当前运行模型信息（供前端「当前运行模型」展示；不含 key）。"""
    provider = (settings.llm_provider or "").strip().lower() or "deepseek"
    model = settings.llm_model.strip() or "deepseek-v4-flash"
    return {
        "provider": provider,
        "model_id": model,
        "base_url": (settings.llm_base_url or "").strip() or None,
        "has_api_key": bool(_resolve_token(_pick_key(settings))),
    }


def has_llm_credentials(settings: Settings) -> bool:
    """当前 provider 是否配置了 secret（smoke 预检）。"""
    return bool(_resolve_token(_pick_key(settings)))


def require_llm_credentials(settings: Settings) -> None:
    """无凭证时抛 MissingLLMCredentialsError（smoke 显式失败）。"""
    if not has_llm_credentials(settings):
        from app.llm.errors import MissingLLMCredentialsError

        raise MissingLLMCredentialsError("llm credentials are not configured")


def _pick_key(settings: Settings):
    provider = (settings.llm_provider or "").strip().lower()
    if provider == "deepseek":
        return settings.deepseek_api_key
    return settings.llm_api_key
