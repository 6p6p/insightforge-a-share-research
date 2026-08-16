"""Application settings, loaded from environment variables and the project .env file."""

import importlib.metadata
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> three levels up is the project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_DATABASE_URL_PREFIX = "postgresql+psycopg://"
_MAX_PORT = 65535
_MAX_TIMEOUT_SECONDS = 60

_MIN_SOURCE_FILE_SIZE_BYTES = 1024 * 1024  # 1 MiB
_MAX_SOURCE_FILE_SIZE_BYTES = 500 * 1024 * 1024  # 500 MiB
_DEFAULT_SOURCE_MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024  # 100 MiB

_MIN_MACRO_JSON_RESPONSE_BYTES = 1024  # 1 KiB
_MAX_MACRO_JSON_RESPONSE_BYTES = 20 * 1024 * 1024  # 20 MiB
_DEFAULT_MACRO_MAX_JSON_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "InsightForge"
    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8001
    log_level: str = "INFO"
    api_v1_prefix: str = "/api/v1"
    # CORS 放行来源（逗号分隔）。默认开发用 Vite frontend origin；
    # 显式来源 + allow_credentials，**禁止** allow_origins=["*"] + credentials=true。
    cors_allow_origins: str = "http://localhost:5173"

    # database_url 不进入 repr，避免密码泄露
    database_url: str = Field(..., repr=False)
    database_echo: bool = False
    database_connect_timeout_seconds: int = 5

    chroma_host: str = "127.0.0.1"
    chroma_port: int = 8002
    chroma_ssl: bool = False
    chroma_timeout_seconds: int = 5

    workflow_shutdown_timeout_seconds: int = 10
    workflow_reconcile_timeout_seconds: int = 5

    # V1.1 P0-1/P0-3：启动时自动执行 Source Registry defaults bootstrap +
    # Company Master bootstrap（幂等、离线、不覆盖既有数据）。测试环境关闭，
    # 避免污染共享测试库（fresh acceptance 测试显式驱动 bootstrap）。
    bootstrap_on_startup: bool = True

    # 本地不可变原始文件存储（开发环境本地磁盘；未来可替换为 S3/MinIO）
    raw_storage_root: Path = PROJECT_ROOT / ".data" / "raw"
    # 导出字节内容寻址存储根（stage 6C；`.data/exports/sha256/<ab>/<cd>/<sha>.<ext>`）
    export_storage_root: Path = PROJECT_ROOT / ".data" / "exports"
    source_max_file_size_bytes: int = _DEFAULT_SOURCE_MAX_FILE_SIZE_BYTES
    # Macro 原始 JSON 响应单文件字节上限（独立于公司 PDF 上限）
    macro_max_json_response_bytes: int = _DEFAULT_MACRO_MAX_JSON_RESPONSE_BYTES

    # LLM runtime（stage 3C.2.1）：provider 由 factory 分派；model 名对应 provider
    # API 的真实模型标识；deepseek_api_key 是 SecretStr，**不进 repr / 日志 /
    # error response / model_id / DB，也不作为 Docker build ARG/ENV**（compose
    # 只做 ${DEEPSEEK_API_KEY:-} 运行时注入）。没有 key 时应用仍允许启动。
    # DeepSeek 官方已停止 legacy model names（deepseek-chat / deepseek-reasoner），
    # 当前统一使用 deepseek-v4-flash。
    llm_provider: str = "deepseek"
    llm_model: str = "deepseek-v4-flash"
    # P0：Default Research Intent Generator 的 optional LLM enhancement 开关
    # （默认关闭 → 纯确定性 template，replay/fingerprint 兼容；开启后同输入
    # 可能生成不同问题 → 新 plan 行，与 planner LLM 非确定性一致）。
    intent_llm_enhancement: bool = False
    # P2：Model Assisted Discovery Node 开关（默认开启 = AUTO 模式；无 API key
    # / 网络失败时安全降级 exhausted）。LLM 生成候选 URL，仍须经 provider 域名
    # allowlist + SafeFetcher 验证后才落库，绝不 bypass provenance。
    search_discovery_llm_enabled: bool = True
    # P4：News Discovery 开关（默认开启 = AUTO 模式；真实 GDELT 网络调用，
    # 失败安全降级 exhausted）。
    news_discovery_enabled: bool = True
    # P3：Company Website/IR Discovery 开关（默认开启 = AUTO 模式；公司官网
    # 有界爬取 issuer_ir_material，失败安全降级 exhausted）。
    ir_discovery_enabled: bool = True
    llm_timeout_seconds: int = 60
    llm_max_retries: int = 1
    deepseek_api_key: SecretStr | None = None

    @field_validator("app_port", "chroma_port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if not 1 <= value <= _MAX_PORT:
            raise ValueError("port must be between 1 and 65535")
        return value

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: str) -> str:
        if not value.startswith(_DATABASE_URL_PREFIX):
            raise ValueError("database_url must start with postgresql+psycopg://")
        return value

    @field_validator(
        "database_connect_timeout_seconds",
        "chroma_timeout_seconds",
        "workflow_shutdown_timeout_seconds",
        "llm_timeout_seconds",
    )
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if not 1 <= value <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout must be between 1 and 60 seconds")
        return value

    @field_validator("llm_max_retries")
    @classmethod
    def _validate_llm_max_retries(cls, value: int) -> int:
        if value < 0 or value > 10:
            raise ValueError("llm_max_retries must be between 0 and 10")
        return value

    @field_validator("deepseek_api_key", mode="before")
    @classmethod
    def _normalize_deepseek_api_key(cls, value):
        """空串 / 空白 → None（未配置）；真实 key 保持原样交给 SecretStr。"""
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("workflow_reconcile_timeout_seconds")
    @classmethod
    def _validate_reconcile_timeout(cls, value: int) -> int:
        if not 1 <= value <= 30:
            raise ValueError("workflow_reconcile_timeout_seconds must be between 1 and 30")
        return value

    @field_validator("source_max_file_size_bytes")
    @classmethod
    def _validate_source_max_file_size(cls, value: int) -> int:
        if not _MIN_SOURCE_FILE_SIZE_BYTES <= value <= _MAX_SOURCE_FILE_SIZE_BYTES:
            raise ValueError("source_max_file_size_bytes must be between 1 MiB and 500 MiB")
        return value

    @field_validator("macro_max_json_response_bytes")
    @classmethod
    def _validate_macro_max_json_response_bytes(cls, value: int) -> int:
        if not _MIN_MACRO_JSON_RESPONSE_BYTES <= value <= _MAX_MACRO_JSON_RESPONSE_BYTES:
            raise ValueError("macro_max_json_response_bytes must be between 1 KiB and 20 MiB")
        return value

    @field_validator("log_level")
    @classmethod
    def _normalize_log_level(cls, value: str) -> str:
        normalized = value.upper()
        if normalized not in _LOG_LEVELS:
            raise ValueError(f"log_level must be one of {sorted(_LOG_LEVELS)}")
        return normalized


def get_package_version() -> str:
    try:
        return importlib.metadata.version("insightforge-backend")
    except importlib.metadata.PackageNotFoundError:
        return "0.1.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
