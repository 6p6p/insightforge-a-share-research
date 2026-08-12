"""Research orchestration runner unit tests（spec I/M/N/O + 7A.2B.2 L/M，0 DB）。

- `run_orchestration` 守卫：orchestration 缺失 → NotFound；terminal →
  AlreadyFinished（不触碰 checkpointer）；
- **awaiting_stage5 continuation（spec M）**：graph 已到 END（`state.next` 空）且
  phase=awaiting_stage5 → `aupdate_state(as_node=ensure_stage5_child)` 重新进入
  `run_or_resume_stage5`；`next` 非空（graph 中途暂停）→ 直接 `astream(None)`；
  waiting_manual 等其它 finished terminal → 不续接；
- 失败投影（spec M）：phase=stage4 → 稳定 error_code `stage4_execution_failed`；
  phase=stage5 → `stage5_execution_failed`；其他 phase → `orchestration_execution_failed`；
  `_sanitize_error` 只留类型名、截断 ≤200，不泄漏 SQL / stack / raw message。
"""

from types import SimpleNamespace
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


class _FakeCheckpointManager:
    async def get_checkpointer(self):
        return object()


class _FakeGraph:
    """fake 编译后 graph：记录 astream / aupdate_state，按测试构造返回 prior。"""

    def __init__(self, *, prior_values=None, prior_next=()) -> None:
        self._prior_values = dict(prior_values) if prior_values else None
        self._prior_next = list(prior_next)
        self.aupdate_calls: list = []
        self.astream_inputs: list = []

    async def aget_state(self, config):
        if self._prior_values is None:
            return None
        return SimpleNamespace(values=dict(self._prior_values), next=list(self._prior_next))

    async def aupdate_state(self, config, values, as_node=None):
        self.aupdate_calls.append((values, as_node))

    async def astream(self, input_state, config, stream_mode="updates"):
        self.astream_inputs.append(input_state)
        if False:
            yield  # pragma: no cover — async generator 形态


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
async def test_failure_projection_stage5_phase(monkeypatch) -> None:
    """Stage5 阶段失败 → error_code=stage5_execution_failed（7A.2B.2 spec M）。"""
    sessionmaker = FakeSessionMaker()
    marked: dict = {}

    async def fake_get(self, orchestration_id):
        return make_orchestration(status="running", current_phase=OrchestrationPhase.STAGE5.value)

    async def fake_mark_failed(self, orchestration_id, completed_at, *, error_code, error_message):
        marked["error_code"] = error_code

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", fake_mark_failed)

    await _runner(sessionmaker)._mark_orchestration_failed(_OID, RuntimeError("boom"))
    assert marked["error_code"] == "stage5_execution_failed"


# ------------------------------------------------------------------ continuation (spec M)


def _patch_graph_builder(monkeypatch, graph: _FakeGraph) -> None:
    monkeypatch.setattr(
        "app.research_orchestration.runner.build_top_level_research_orchestration_graph",
        lambda deps, checkpointer: graph,
    )


async def _run_continuation(monkeypatch, *, orchestration, graph: _FakeGraph) -> _FakeGraph:
    sessionmaker = FakeSessionMaker()

    async def fake_get(self, orchestration_id):
        return orchestration

    async def fake_update_progress(self, orchestration_id, *, status, current_phase):
        return SimpleNamespace(orchestration_id=orchestration_id)

    monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", fake_get)
    monkeypatch.setattr(ResearchOrchestrationRepository, "update_progress", fake_update_progress)
    _patch_graph_builder(monkeypatch, graph)
    await _runner(sessionmaker, checkpoint_manager=_FakeCheckpointManager()).run_orchestration(_OID)
    return graph


@pytest.mark.asyncio
async def test_continuation_awaiting_stage5_reinjects(monkeypatch) -> None:
    """graph 已到 END + phase=awaiting_stage5 → aupdate_state(as_node=ensure_stage5_child)。"""
    graph = _FakeGraph(prior_values={"current_phase": "awaiting_stage5"}, prior_next=())
    orchestration = make_orchestration(status="running", current_phase="awaiting_stage5")
    graph = await _run_continuation(monkeypatch, orchestration=orchestration, graph=graph)

    assert graph.aupdate_calls == [({"current_phase": "stage5"}, "ensure_stage5_child")]
    assert graph.astream_inputs == [None]


@pytest.mark.asyncio
async def test_continuation_resumes_pending_next_directly(monkeypatch) -> None:
    """`state.next` 非空（graph 中途暂停 / crash）→ 直接 astream(None)，不续接。"""
    graph = _FakeGraph(
        prior_values={"current_phase": "stage4"}, prior_next=["run_or_resume_stage4"]
    )
    orchestration = make_orchestration(status="running", current_phase="stage4")
    graph = await _run_continuation(monkeypatch, orchestration=orchestration, graph=graph)

    assert graph.aupdate_calls == []
    assert graph.astream_inputs == [None]


@pytest.mark.asyncio
async def test_continuation_finished_waiting_manual_noop(monkeypatch) -> None:
    """graph 已到 END 但 phase=waiting_manual → 不续接（等人工补齐，无 Stage5 child）。"""
    graph = _FakeGraph(prior_values={"current_phase": "waiting_manual"}, prior_next=())
    orchestration = make_orchestration(status="waiting_human", current_phase="waiting_manual")
    graph = await _run_continuation(monkeypatch, orchestration=orchestration, graph=graph)

    assert graph.aupdate_calls == []
    assert graph.astream_inputs == [None]


@pytest.mark.asyncio
async def test_fresh_start_builds_initial_state(monkeypatch) -> None:
    """无 checkpoint → 初始 state 首启（planning）。"""
    graph = _FakeGraph()  # prior_values=None → aget_state 返回 None
    orchestration = make_orchestration(status="running", current_phase="planning")
    graph = await _run_continuation(monkeypatch, orchestration=orchestration, graph=graph)

    assert graph.aupdate_calls == []
    assert len(graph.astream_inputs) == 1
    initial = graph.astream_inputs[0]
    assert initial["orchestration_id"] == str(_OID)
    assert initial["current_phase"] == "planning"


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
