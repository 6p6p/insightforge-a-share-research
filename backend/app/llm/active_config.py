"""LLM runtime active-config resolution (v1.2.7-B).

优先级：数据库 active 配置 > 环境变量 > 默认。

现有 production adapter 只认证 DeepSeek（component-inventory 静态扫描约束）。
因此 DB active 配置仅在 provider=="deepseek" 时覆盖 Settings 的
llm_provider / llm_model / deepseek_api_key（模型与密钥从应用层配置生效）；
openai / openrouter / custom 的配置可存储、展示与测试连接，但为避免破坏已
审计的 deepseek-only 研究执行路径，研究流程暂不改用它们（仍走 .env 默认）。
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
    """把 active 配置覆盖到 settings（仅 deepseek provider）。返回是否应用。"""
    if active is None:
        return False
    provider = (getattr(active, "provider", "") or "").strip().lower()
    if provider != "deepseek":
        return False
    model_id = (getattr(active, "model_id", "") or "").strip()
    if not model_id:
        return False
    object.__setattr__(settings, "llm_provider", "deepseek")
    object.__setattr__(settings, "llm_model", model_id)
    encrypted = getattr(active, "encrypted_api_key", None)
    if encrypted:
        key = LlmConfigKeyStore(settings).decrypt(encrypted)
        if key:
            from pydantic import SecretStr

            object.__setattr__(settings, "deepseek_api_key", SecretStr(key))
    return True
