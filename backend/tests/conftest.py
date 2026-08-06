"""Shared fixtures for the backend test suite."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

_APP_ENV_KEYS = ("APP_NAME", "APP_ENV", "APP_HOST", "APP_PORT", "LOG_LEVEL", "API_V1_PREFIX")


@pytest.fixture(autouse=True)
def _clean_app_env(monkeypatch):
    """Isolate tests from any APP_* variables in the ambient environment."""
    for name in _APP_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def test_settings() -> Settings:
    return Settings(_env_file=None, app_env="test", app_name="InsightForge", log_level="DEBUG")


@pytest.fixture
def app(test_settings: Settings):
    return create_app(test_settings)


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
