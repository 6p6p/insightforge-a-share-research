"""Research orchestration runner unit tests（spec I/M/N/O，0 DB）。

- `run_orchestration` 守卫：orchestration 缺失 → NotFound；terminal →
  AlreadyFinished（不触碰 checkpointer）；
- 失败投影（spec M）：phase=stage4 → 稳定 error_code `stage4_execution_failed`；
  其他 phase → `orchestration_execution_failed`；`_sanitize_error` 只留类型名、
  截断 ≤200，不泄漏 SQL / stack / raw message。
"""

from uuid import UUID

import pytest

from app.research_orchestration.contracts import OrchestrationPhase, OrchestrationStatus
from app.research_orchestration.errors import (
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationNotFound,
)
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.research_orchestration.runner import (
    ResearchOrchestrationRunner,
    _sanitize_error,
)
from tests.research_orchestration.fakes import FakeSessionMaker, make_orchestration

_OID = UUID("00000000-0000-0000-0000-000000000001")


def _runner(sessionmaker, *, checkpoint_manager=None):
    return ResearchOrchestrationRunner(sessionmaker, checkpoint_manager, dependencies=None)


# ------------------------------------------------------------------ sanitize


def test_sanitize_error_keeps_type_name_only() -> None:
    assert _sanitize_error(ValueError("secret sql dump")) == "ValueError"


def test_sanitize_error_truncates_to_200() -> None:
    # 构造一个类型名超过 200 字符的异常类型。
    long_type = type("A" * 300, (ValueError,), {})
    assert len(_sanitize_error(long_type("x"))) <= 200


def test_sanitize_error_stable_no_throw() -> None:
    assert _sanitize_error(RuntimeError()) == "RuntimeError"


# ------------------------------------------------------------------ guards


@pytest.mark.asyncio
async def test_run_orchestration_missing_raises_not_found(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()

    async def fake_get(self, orchestration_id):
        return None

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationNotFound):
        await _runner(sessionmaker).run_orchestration(_OID)


@pytest.mark.asyncio
async def test_run_orchestration_terminal_raises_already_finished(monkeypatch) -> None:
    sessionmaker = FakeSessionMaker()

    async def fake_get(self, orchestration_id):
        return make_orchestration(status=OrchestrationStatus.FAILED.value, current_phase="stage4")

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await _runner(sessionmaker).run_orchestration(_OID)


# ------------------------------------------------------------------ failure projection


@pytest.mark.asyncio
async def test_failure_projection_stage4_phase(monkeypatch) -> None:
    """child 阶段失败 → error_code=stage4_execution_failed，phase 保持 stage4。"""
    sessionmaker = FakeSessionMaker()
    marked: dict = {}

    async def fake_get(self, orchestration_id):
        return make_orchestration(status="running", current_phase=OrchestrationPhase.STAGE4.value)

    async def fake_mark_failed(self, orchestration_id, completed_at, *, error_code, error_message):
        marked["error_code"] = error_code
        marked["error_message"] = error_message

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", fake_mark_failed)

    runner = _runner(sessionmaker)
    await runner._mark_orchestration_failed(_OID, ValueError("boom"))
    assert marked["error_code"] == "stage4_execution_failed"
    assert marked["error_message"] == "ValueError"
    assert sessionmaker.session.committed is True


@pytest.mark.asyncio
async def test_failure_projection_non_stage4_phase(monkeypatch) -> None:
    """planning 等非 stage4 阶段失败 → orchestration_execution_failed。"""
    sessionmaker = FakeSessionMaker()
    marked: dict = {}

    async def fake_get(self, orchestration_id):
        return make_orchestration(status="running", current_phase=OrchestrationPhase.PLANNING.value)

    async def fake_mark_failed(self, orchestration_id, completed_at, *, error_code, error_message):
        marked["error_code"] = error_code

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", fake_mark_failed)

    await _runner(sessionmaker)._mark_orchestration_failed(_OID, RuntimeError("boom"))
    assert marked["error_code"] == "orchestration_execution_failed"


@pytest.mark.asyncio
async def test_failure_projection_missing_row_defaults_planning(monkeypatch) -> None:
    """orchestration 行缺失（恢复边界）→ 按 planning 投影，不 500。"""
    sessionmaker = FakeSessionMaker()
    marked: dict = {}

    async def fake_get(self, orchestration_id):
        return None

    async def fake_mark_failed(self, orchestration_id, completed_at, *, error_code, error_message):
        marked["error_code"] = error_code

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", fake_mark_failed)

    await _runner(sessionmaker)._mark_orchestration_failed(_OID, RuntimeError("boom"))
    assert marked["error_code"] == "orchestration_execution_failed"
