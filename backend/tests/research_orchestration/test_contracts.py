"""Research orchestration contracts unit tests（spec F fingerprint，0 DB）。

`compute_orchestration_input_fingerprint` 必须：确定（同输入 → 同 fp）、canonical
（sort_keys + 固定 separators）、**组合敏感**（schema / task_id / planner input
fingerprint / orchestrator 身份任一变化 → 新 fp）、**时间无关**（不含 API key /
created_at / row identity）。
"""

import hashlib
import json
from uuid import UUID

from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
    ChildStage,
    OrchestrationPhase,
    OrchestrationStatus,
    compute_orchestration_input_fingerprint,
)

_TASK_ID = UUID("11111111-1111-1111-1111-111111111111")
_PLANNER_FP = "p" * 64


def _fingerprint(**overrides) -> str:
    kwargs = {
        "orchestration_schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "task_id": _TASK_ID,
        "planner_input_fingerprint": _PLANNER_FP,
        "orchestrator_name": ORCHESTRATOR_NAME,
        "orchestrator_version": ORCHESTRATOR_VERSION,
    }
    kwargs.update(overrides)
    return compute_orchestration_input_fingerprint(**kwargs)


def test_fingerprint_deterministic() -> None:
    assert _fingerprint() == _fingerprint()


def test_fingerprint_matches_canonical_sha256() -> None:
    payload = {
        "orchestration_schema_version": ORCHESTRATION_SCHEMA_VERSION,
        "task_id": str(_TASK_ID),
        "planner_input_fingerprint": _PLANNER_FP,
        "orchestrator": {"name": ORCHESTRATOR_NAME, "version": ORCHESTRATOR_VERSION},
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    assert _fingerprint() == expected


def test_fingerprint_is_64_hex_chars() -> None:
    assert len(_fingerprint()) == 64
    assert set(_fingerprint()) <= set("0123456789abcdef")


def test_fingerprint_changes_when_task_id_changes() -> None:
    assert _fingerprint(task_id=UUID("22222222-2222-2222-2222-222222222222")) != _fingerprint()


def test_fingerprint_changes_when_planner_input_changes() -> None:
    # 改 task 研究问题 → 新 planner input fingerprint → 新 orchestration fingerprint
    # （spec P：真正的 user retry 必须产生新 fingerprint，不是复活同一行）。
    assert _fingerprint(planner_input_fingerprint="q" * 64) != _fingerprint()


def test_fingerprint_changes_when_schema_changes() -> None:
    assert _fingerprint(orchestration_schema_version=2) != _fingerprint()


def test_fingerprint_changes_when_orchestrator_identity_changes() -> None:
    assert _fingerprint(orchestrator_name="other_orchestrator") != _fingerprint()
    assert _fingerprint(orchestrator_version=2) != _fingerprint()


def test_fingerprint_time_independent() -> None:
    # fingerprint 不含 created_at / row identity：两次调用结果一致。
    assert _fingerprint() == _fingerprint()


def test_enum_values_stable() -> None:
    assert set(OrchestrationStatus) == {
        "pending",
        "running",
        "waiting_human",
        "completed",
        "completed_with_warnings",
        "failed",
        "cancelled",
    }
    assert OrchestrationPhase.AWAITING_STAGE5.value == "awaiting_stage5"
    assert OrchestrationPhase.WAITING_MANUAL.value == "waiting_manual"
    assert ChildStage.STAGE4.value == "stage4"
    assert ChildStage.STAGE5.value == "stage5"
