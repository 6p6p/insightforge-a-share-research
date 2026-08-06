"""Application settings, loaded from environment variables and the project .env file."""

import importlib.metadata
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# backend/app/core/config.py -> three levels up is the project root
PROJECT_ROOT = Path(__file__).resolve().parents[3]

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


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

    @field_validator("app_port")
    @classmethod
    def _validate_port(cls, value: int) -> int:
        if not 1 <= value <= 65535:
            raise ValueError("app_port must be between 1 and 65535")
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
