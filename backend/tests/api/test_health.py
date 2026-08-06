"""Tests for the health check endpoints."""

from app.schemas.health import ReadyHealthResponse


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


def test_ready_environment_from_test_settings(client) -> None:
    response = client.get("/api/v1/health/ready")
    body = response.json()
    assert body["environment"] == "test"
    assert body["service"] == "insightforge-backend"
    assert body["version"] == "0.1.0"


def test_x_request_id_is_auto_generated(client) -> None:
    response = client.get("/api/v1/health/live")
    request_id = response.headers.get("X-Request-ID")
    assert request_id is not None
    assert len(request_id) == 36


def test_x_request_id_is_preserved(client) -> None:
    request_id = "test-request-id-abc"
    response = client.get("/api/v1/health/live", headers={"X-Request-ID": request_id})
    assert response.headers["X-Request-ID"] == request_id


def test_openapi_contains_health_endpoints(client) -> None:
    schema = client.get("/openapi.json").json()
    assert "/api/v1/health/live" in schema["paths"]
    assert "/api/v1/health/ready" in schema["paths"]
