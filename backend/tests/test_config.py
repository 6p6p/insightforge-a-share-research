"""Tests for application settings loading and validation."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_default_config() -> None:
    settings = Settings(_env_file=None)
    assert settings.app_name == "InsightForge"
    assert settings.app_env == "development"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 8001
    assert settings.log_level == "INFO"
    assert settings.api_v1_prefix == "/api/v1"


def test_environment_variables_override_defaults(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "Override")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_PORT", "9000")
    settings = Settings(_env_file=None)
    assert settings.app_name == "Override"
    assert settings.app_env == "production"
    assert settings.app_port == 9000


def test_port_string_is_coerced_to_int(monkeypatch) -> None:
    monkeypatch.setenv("APP_PORT", "8002")
    settings = Settings(_env_file=None)
    assert settings.app_port == 8002
    assert isinstance(settings.app_port, int)


def test_invalid_port_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, app_port=70000)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, log_level="VERBOSE")


def test_local_dotenv_ignored_when_env_file_none() -> None:
    settings = Settings(_env_file=None, app_env="test")
    assert settings.app_env == "test"
