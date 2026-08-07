"""Shared fixtures for the backend test suite."""

import httpx
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

# 测试级网络隔离放行名单：本地回环地址不受 guard 拦截，
# 保证 PostgreSQL、Docker Chroma、FastAPI TestClient 的本地连接不受影响。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})


@pytest.fixture(autouse=True)
def _forbid_real_http(monkeypatch):
    """测试级真实外网隔离：任何非回环的真实 httpx transport 请求都会失败。

    - httpx.MockTransport（AsyncBaseTransport）自带 handle_async_request，不受影响；
    - FastAPI TestClient 走 ASGI transport，不受影响；
    - PostgreSQL（psycopg 驱动）、Docker Chroma（chromadb 内部 httpx 走 127.0.0.1）
      均在回环放行名单内，不受影响；
    - 任何 Provider/acquisition/source ingestion 测试忘记注入 MockTransport
      而发起真实外部请求时立即失败。
    """
    original = httpx.AsyncHTTPTransport.handle_async_request

    async def _forbid(self, request, *args, **kwargs):
        host = getattr(request, "url", None)
        hostname = host.host if host is not None else None
        if hostname in _LOOPBACK_HOSTS:
            return await original(self, request, *args, **kwargs)
        raise AssertionError("real external HTTP is forbidden in tests")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", _forbid)


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
