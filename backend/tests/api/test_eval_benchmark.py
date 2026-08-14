"""Eval benchmark summary endpoint tests (stage 7B.1.4E / §25 web view).

只读端点：合法 payload 原样返回；缺失 run → 404；非法 run 模式 → 404；
workspace 可经 EVAL_BENCHMARK_WORKSPACE 覆盖（测试隔离，不碰 repo 产物）。
"""

import json

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    # 隔离 workspace：不读取 repo benchmark/ 产物。
    run_dir = tmp_path / "run_fake"
    run_dir.mkdir(parents=True)
    payload = {
        "dataset_id": "insightforge_a_share_benchmark",
        "dataset_version": 1,
        "as_of": "2025-08-01",
        "mode": "fake",
        "model": "deepseek:deepseek-v4-flash",
        "generated_at": "2026-08-14T00:00:00+00:00",
        "attempts": [
            {
                "case_id": "moutai-business",
                "variant_id": "single_rag",
                "attempt_no": 1,
                "mode": "fake",
                "status": "success",
                "error_code": None,
                "wall_latency_ms": 662,
                "execution_id": "e" * 32,
                "variant_output_fingerprint": "f" * 64,
                "usage_components": ["eval_single_rag_answer"],
                "usage_call_count": 1,
                "total_tokens": 40,
                "estimated_cost_usd": "0.0000274",
                "citation_validity": {
                    "status": "computed",
                    "value": "1.0",
                    "numerator": "1",
                    "denominator": "1",
                },
                "citation_coverage": {
                    "status": "computed",
                    "value": "1.0",
                    "numerator": "1",
                    "denominator": "1",
                },
                "persisted": True,
                "expected_fail_fast": False,
                "notes": [],
            }
        ],
    }
    (run_dir / "results.json").write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("EVAL_BENCHMARK_WORKSPACE", str(tmp_path))
    app = create_app()
    with TestClient(app) as test_client:
        yield test_client


def test_summary_returns_payload(client) -> None:
    response = client.get("/api/v1/eval/benchmark/summary?run=fake")
    assert response.status_code == 200
    body = response.json()
    assert body["dataset_id"] == "insightforge_a_share_benchmark"
    assert len(body["attempts"]) == 1
    assert body["attempts"][0]["status"] == "success"
    assert body["attempts"][0]["citation_validity"]["value"] == "1.0"


def test_summary_missing_run_returns_404(client) -> None:
    response = client.get("/api/v1/eval/benchmark/summary?run=real")
    assert response.status_code == 404
    assert "benchmark run 结果不存在" in response.json()["detail"]


def test_summary_invalid_mode_returns_404(client) -> None:
    response = client.get("/api/v1/eval/benchmark/summary?run=other")
    assert response.status_code == 404
