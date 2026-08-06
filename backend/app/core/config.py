"""Application settings, loaded from environment variables and the project .env file."""

import importlib.metadata
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> three levels up is the project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_DATABASE_URL_PREFIX = "postgresql+psycopg://"
_MAX_PORT = 65535
_MAX_TIMEOUT_SECONDS = 60


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

    # database_url 不进入 repr，避免密码泄露
    database_url: str = Field(..., repr=False)
    database_echo: bool = False
    database_connect_timeout_seconds: int = 5

    chroma_host: str = "127.0.0.1"
    chroma_port: int = 8002
    chroma_ssl: bool = False
    chroma_timeout_seconds: int = 5

    workflow_shutdown_timeout_seconds: int = 10

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
    )
    @classmethod
    def _validate_timeout(cls, value: int) -> int:
        if not 1 <= value <= _MAX_TIMEOUT_SECONDS:
            raise ValueError("timeout must be between 1 and 60 seconds")
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
