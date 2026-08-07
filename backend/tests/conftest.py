"""Shared fixtures for the backend test suite."""

import pytest
from fastapi.testclient import TestClient

from app.api.dependencies import (
    get_langgraph_checkpoint_manager,
    get_raw_storage,
)
from app.core.config import Settings
from app.core.errors import SourceStorageUnavailable
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


class FakeLanggraph:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.ready_calls = 0

    async def check_ready(self) -> None:
        self.ready_calls += 1
        if not self.healthy:
            raise RuntimeError("checkpoint schema unavailable")


class FakeRawStore:
    def __init__(self, healthy: bool = True) -> None:
        self.healthy = healthy
        self.check_calls = 0

    def check_ready(self) -> None:
        self.check_calls += 1
        if not self.healthy:
            raise SourceStorageUnavailable()


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
def fake_langgraph() -> FakeLanggraph:
    return FakeLanggraph()


@pytest.fixture
def fake_raw_store() -> FakeRawStore:
    return FakeRawStore()


@pytest.fixture
def app(
    test_settings: Settings,
    fake_database: FakeDatabase,
    fake_chroma: FakeChroma,
    fake_langgraph: FakeLanggraph,
    fake_raw_store: FakeRawStore,
):
    application = create_app(test_settings)
    application.dependency_overrides[get_database] = lambda: fake_database
    application.dependency_overrides[get_chroma] = lambda: fake_chroma
    application.dependency_overrides[get_langgraph_checkpoint_manager] = lambda: fake_langgraph
    application.dependency_overrides[get_raw_storage] = lambda: fake_raw_store
    return application


@pytest.fixture
def client(app):
    with TestClient(app) as test_client:
        yield test_client
