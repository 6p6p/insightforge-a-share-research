"""Tests for application settings loading and validation."""

import pytest
from pydantic import ValidationError

from app.core.config import Settings

_DB_URL = "postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge"


def test_default_config() -> None:
    settings = Settings(_env_file=None, database_url=_DB_URL)
    assert settings.app_name == "InsightForge"
    assert settings.app_env == "development"
    assert settings.app_host == "0.0.0.0"
    assert settings.app_port == 8001
    assert settings.log_level == "INFO"
    assert settings.api_v1_prefix == "/api/v1"
    assert settings.database_echo is False
    assert settings.database_connect_timeout_seconds == 5
    assert settings.chroma_host == "127.0.0.1"
    assert settings.chroma_port == 8002
    assert settings.chroma_ssl is False
    assert settings.chroma_timeout_seconds == 5


def test_environment_variables_override_defaults(monkeypatch) -> None:
    monkeypatch.setenv("APP_NAME", "Override")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("APP_PORT", "9000")
    monkeypatch.setenv("CHROMA_HOST", "chroma.internal")
    settings = Settings(_env_file=None, database_url=_DB_URL)
    assert settings.app_name == "Override"
    assert settings.app_env == "production"
    assert settings.app_port == 9000
    assert settings.chroma_host == "chroma.internal"


def test_port_string_is_coerced_to_int(monkeypatch) -> None:
    monkeypatch.setenv("APP_PORT", "8002")
    settings = Settings(_env_file=None, database_url=_DB_URL)
    assert settings.app_port == 8002
    assert isinstance(settings.app_port, int)


def test_invalid_port_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=_DB_URL, app_port=70000)


def test_invalid_log_level_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=_DB_URL, log_level="VERBOSE")


def test_database_url_valid() -> None:
    settings = Settings(_env_file=None, database_url=_DB_URL)
    assert settings.database_url == _DB_URL


def test_invalid_database_scheme_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url="postgresql://user:pass@host:5432/db")


def test_invalid_chroma_port_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=_DB_URL, chroma_port=0)


def test_invalid_timeout_rejected() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, database_url=_DB_URL, chroma_timeout_seconds=61)


def test_settings_repr_hides_database_password() -> None:
    secret = "supersecret"
    settings = Settings(
        _env_file=None,
        database_url=f"postgresql+psycopg://user:{secret}@host:5432/db",
    )
    representation = repr(settings)
    assert secret not in representation
    assert "postgresql+psycopg" not in representation


def test_local_dotenv_ignored_when_env_file_none() -> None:
    settings = Settings(_env_file=None, database_url=_DB_URL, app_env="test")
    assert settings.app_env == "test"
