"""Tests for the health check endpoints."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.db.session import DatabaseManager
from app.main import create_app
from app.schemas.health import ReadyHealthResponse
from app.storage.raw_store import LocalRawArtifactStore


def test_live_returns_ok(client) -> None:
    response = client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_ready_returns_200(client) -> None:
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 200


def test_ready_matches_structured_model(client) -> None:
    response = client.get("/api/v1/health/ready")
    payload = ReadyHealthResponse.model_validate(response.json())
    assert payload.status == "ok"
    assert payload.checks.configuration == "ok"
    assert payload.checks.database == "ok"
    assert payload.checks.chroma == "ok"
    assert payload.checks.checkpoint == "ok"
    assert payload.checks.raw_storage == "ok"


def test_ready_environment_from_test_settings(client) -> None:
    response = client.get("/api/v1/health/ready")
    body = response.json()
    assert body["environment"] == "test"
    assert body["service"] == "insightforge-backend"
    assert body["version"] == "0.1.0"


def test_ready_database_failure_returns_503(client, fake_database) -> None:
    fake_database.healthy = False
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["configuration"] == "ok"
    assert body["checks"]["database"] == "error"
    assert body["checks"]["chroma"] == "ok"
    assert body["checks"]["checkpoint"] == "ok"
    assert body["checks"]["raw_storage"] == "ok"


def test_ready_checkpoint_failure_returns_503(client, fake_langgraph) -> None:
    fake_langgraph.healthy = False
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["checkpoint"] == "error"


def test_ready_chroma_failure_returns_503(client, fake_chroma) -> None:
    fake_chroma.healthy = False
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["checks"]["chroma"] == "error"


def test_ready_raw_storage_failure_returns_503(client, fake_raw_store) -> None:
    fake_raw_store.healthy = False
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["raw_storage"] == "error"
    assert body["checks"]["database"] == "ok"


def test_ready_raw_storage_delete_failure_returns_503_hiding_paths(
    app, monkeypatch, tmp_path
) -> None:
    """删除探测文件失败 → 503、raw_storage=error，且响应不含任何绝对路径。"""
    import os as _os
    from pathlib import Path as _Path

    from app.api.dependencies import get_raw_storage

    store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    real_unlink = _os.unlink

    def fail_unlink(path) -> None:
        if _Path(path).name.startswith(".ready-"):
            raise OSError("unlink failed")
        return real_unlink(path)

    monkeypatch.setattr(_os, "unlink", fail_unlink)
    app.dependency_overrides[get_raw_storage] = lambda: store
    with TestClient(app) as test_client:
        response = test_client.get("/api/v1/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert body["checks"]["raw_storage"] == "error"
        assert str(tmp_path) not in response.text
        assert ".ready-" not in response.text


def test_ready_both_failures_returns_503(client, fake_database, fake_chroma) -> None:
    fake_database.healthy = False
    fake_chroma.healthy = False
    response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database"] == "error"
    assert body["checks"]["chroma"] == "error"
    assert body["checks"]["checkpoint"] == "ok"


def test_ready_response_hides_exception_details(client, fake_database) -> None:
    fake_database.healthy = False
    response = client.get("/api/v1/health/ready")
    text = response.text
    assert "postgres unavailable" not in text
    assert "ConnectionError" not in text
    assert "postgresql+psycopg" not in text


def test_live_does_not_probe_dependencies(
    client, fake_database, fake_chroma, fake_raw_store
) -> None:
    client.get("/api/v1/health/live")
    assert fake_database.ping_calls == 0
    assert fake_chroma.heartbeat_calls == 0
    assert fake_raw_store.check_calls == 0


def test_lifespan_creates_resources(test_settings) -> None:
    application = create_app(test_settings)
    with TestClient(application) as test_client:
        resources = test_client.app.state.resources
        assert resources is not None
        assert isinstance(resources.database, DatabaseManager)
        assert resources.chroma is not None
        assert isinstance(resources.raw_storage, LocalRawArtifactStore)
        assert resources.raw_storage._root == Path(test_settings.raw_storage_root)
    assert application.state.resources is None


def test_lifespan_disposes_database(monkeypatch, test_settings) -> None:
    disposed = []
    original = DatabaseManager.dispose

    async def spy(self) -> None:
        disposed.append(True)
        await original(self)

    monkeypatch.setattr(DatabaseManager, "dispose", spy)
    application = create_app(test_settings)
    with TestClient(application):
        pass
    assert disposed


def test_x_request_id_is_auto_generated(client) -> None:
    response = client.get("/api/v1/health/live")
    request_id = response.headers.get("X-Request-ID")
    assert request_id is not None
    assert len(request_id) == 36


def test_x_request_id_is_preserved(client) -> None:
    request_id = "test-request-id-abc"
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": request_id})
    assert response.headers["X-Request-ID"] == request_id


def test_x_request_id_oversized_regenerated(client) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": "x" * 200})
    assert len(response.headers["X-Request-ID"]) == 36


def test_x_request_id_non_printable_regenerated(client) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": "abc\x01def"})
    assert len(response.headers["X-Request-ID"]) == 36


def test_x_request_id_empty_regenerated(client) -> None:
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": ""})
    assert len(response.headers["X-Request-ID"]) == 36


def test_openapi_contains_health_endpoints(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/health/live" in schema["paths"]
    assert "/api/v1/health/ready" in schema["paths"]
