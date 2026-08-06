"""Shared fixtures for the backend test suite."""

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.db.dependencies import get_database
from app.main import create_app
from app.vectorstore.dependencies import get_chroma

_APP_ENV_KEYS = ("APP_NAME", "APP_ENV", "APP_HOST", "APP_PORT", "LOG_LEVEL", "API_V1_PREFIX")


class FakeDatabase:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.ping_calls = 0

    async def ping(self) -> None:
        self.ping_calls += 1
        if not self.healthy:
            raise ConnectionError("postgres unavailable")


class FakeChroma:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.heartbeat_calls = 0

    async def heartbeat(self) -> None:
        self.heartbeat_calls += 1
        if not self.healthy:
            raise ConnectionError("chroma unavailable")


@pytest.fixture(autouse=True)
def _clean_app_env(monkeypatch):
    """Isolate tests from any APP_* variables in the ambient environment."""
    for name in _APP_ENV_KEYS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def test_settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        app_name="InsightForge",
        log_level="DEBUG",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )


@pytest.fixture
def fake_database() -> FakeDatabase:
    return FakeDatabase()


@pytest.fixture
def fake_chroma() -> FakeChroma:
    return FakeChroma()


@pytest.fixture
def app(test_settings: Settings, fake_database: FakeDatabase, fake_chroma: FakeChroma):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
