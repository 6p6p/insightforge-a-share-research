"""Runtime active LLM config resolution (v1.2.7-B / v1.2.8).

优先级：数据库 active 配置 > 环境变量 > 默认。

v1.2.8：任意受支持 provider 的 active 配置都会覆盖 Settings（provider /
model / base_url / api key），研究执行统一走 get_active_llm() 读取；
无 DB active 配置时完全 fallback 到 .env（现状不变）。
"""

from app.core.config import Settings
from app.services.llm_provider_config_service import LlmConfigKeyStore


async def load_active_config(sessionmaker) -> object | None:
    """从 llm_provider_configs 表读取 active 配置行；表缺失/异常 → None。"""
    if sessionmaker is None:
        return None
    try:
        from app.repositories.llm_provider_config_repository import (
            LlmProviderConfigRepository,
        )

        async with sessionmaker() as session:
            rows = await LlmProviderConfigRepository(session).list_all()
    except Exception:
        return None
    for row in rows:
        if getattr(row, "is_active", False):
            return row
    return None


def apply_active_override(settings: Settings, active) -> bool:
    """把 active 配置覆盖到 settings（任意受支持 provider）。返回是否应用。

    deepseek → settings.deepseek_api_key；其他 provider → settings.llm_api_key；
    base_url 覆盖 settings.llm_base_url。
    """
    if active is None:
        return False
    provider = (getattr(active, "provider", "") or "").strip().lower()
    if not provider:
        return False
    model_id = (getattr(active, "model_id", "") or "").strip()
    if not model_id:
        return False
    object.__setattr__(settings, "llm_provider", provider)
    object.__setattr__(settings, "llm_model", model_id)
    base_url = (getattr(active, "base_url", None) or "").strip()
    if base_url:
        object.__setattr__(settings, "llm_base_url", base_url)
    encrypted = getattr(active, "encrypted_api_key", None)
    if encrypted:
        key = LlmConfigKeyStore(settings).decrypt(encrypted)
        if key:
            from pydantic import SecretStr

            if provider == "deepseek":
                object.__setattr__(settings, "deepseek_api_key", SecretStr(key))
            else:
                object.__setattr__(settings, "llm_api_key", SecretStr(key))
    return True
