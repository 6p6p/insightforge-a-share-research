"""Real compiled graph execution tests（7A.2B.2 spec L/M，0 DB）。

test_graph.py 只做结构烟测（节点集合 + 纯 state 路由）；runner 的 continuation
在 test_runner.py 用 `_FakeGraph` mock。**这里用真实编译的 LangGraph +
InMemorySaver + fake deps 证明 spec M 的 continuation 真机语义**：

- `run_orchestration` 首启跑到 `awaiting_stage5`（graph 到 END、orchestration
  status=waiting_human、phase=awaiting_stage5、恰好 1 次 execute_stage5）；
- 人工裁决 Stage5 child 后再次 `run_orchestration`：`aupdate_state(config,
  {"current_phase": stage5}, as_node="ensure_stage5_child")` 把 `next` 重新指向
  `run_or_resume_stage5`，节点重查 child 终态注入 fresh `stage5_run_status` →
  `route_stage5_result` 重新路由（completed → complete / research_required →
  pause / 仍 waiting_human → 再次 pause）——**不重建 child、不重复 execute、
  同 thread 同 orchestration**（spec O）；
- research_required / failed / cancelled 三条终态路由在同一 harness 上验证。

InMemorySaver 实例跨两次 run 共享（模拟 PG Checkpointer 持久化）；
`thread_id=orchestration_id`。0 DB / 0 network / 0 real DeepSeek。
"""

from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import OrchestrationPhase, OrchestrationStatus
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.errors import ResearchOrchestrationAlreadyFinished
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.research_orchestration.runner import ResearchOrchestrationRunner
from app.research_orchestration.service import ChildRunResult
from app.stage5.contracts import (
    STAGE5_TERMINAL_CANCELLED,
    STAGE5_TERMINAL_FINALIZE,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
)
from tests.research_orchestration.fakes import (
    FakePlanService,
    FakeSessionMaker,
    make_orchestration,
)

_ORCH_ID = UUID("00000000-0000-0000-0000-000000000001")
_TASK_ID = UUID("00000000-0000-0000-0000-000000000002")
_COMPANY_ID = UUID("00000000-0000-0000-0000-000000000003")
_SYNTHESIS_ID = UUID("00000000-0000-0000-0000-000000000004")
_SYNTHESIS_RESULT_ID = UUID("00000000-0000-0000-0000-000000000005")
_RESEARCH_REQUEST_ID = UUID("00000000-0000-0000-0000-000000000006")

# Stage4 checkpoint state：collect_synthesis（synthesis_id/synthesis_result_id）+
# _stage5_request（company_id/research_question/analysis_as_of）共用同一投影。
_STAGE4_STATE = {
    "company_id": str(_COMPANY_ID),
    "research_question": "分析目标公司基本面",
    "analysis_as_of": "2026-06-30",
    "synthesis_id": str(_SYNTHESIS_ID),
    "synthesis_result_id": str(_SYNTHESIS_RESULT_ID),
}

_STAGE5_OUTCOME_STATUS = {
    "waiting_human": "waiting_human",
    "completed": "completed",
    "research_required": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
}


class _Runs:
    """共享的 workflow_runs 行（monkeypatch get_by_id 读这里）。"""

    def __init__(self) -> None:
        self.runs: dict[str, dict] = {}

    def add(self, run_id: UUID, *, status: str) -> None:
        self.runs[str(run_id)] = {"status": status}

    def set_status(self, run_id: UUID, status: str) -> None:
        self.runs[str(run_id)]["status"] = status

    def get(self, run_id: UUID) -> dict | None:
        return self.runs.get(str(run_id))


class _SharedSaver:
    """跨两次 run 共享 InMemorySaver（模拟 PG Checkpointer 持久化）。"""

    def __init__(self) -> None:
        self._saver = InMemorySaver()

    async def get_checkpointer(self):
        return self._saver


class _FakeRouter:
    async def route_research_plan(self, research_plan_id) -> None:
        return None


class _FakePreparation:
    def __init__(self, *, ready: bool = True) -> None:
        self.ready = ready
        self._stage4_request = SimpleNamespace()

    async def prepare_research(self, research_plan_id):
        return SimpleNamespace(
            ready_for_analysis=self.ready,
            missing_needs=[],
            stage4_request=self._stage4_request,
        )


class _FakeFulfillment:
    async def fulfill_research_needs(self, research_plan_id) -> None:
        return None


class _FakeSynthesis:
    async def verify_synthesis_integrity(self, session, synthesis_id) -> None:
        return None


class _FakeStage4Runner:
    def __init__(self, runs: _Runs, stage4_run_id: UUID) -> None:
        self._runs = runs
        self._run_id = stage4_run_id
        self.execute_calls = 0

    async def read_checkpoint_state(self, run_id) -> dict:
        return dict(_STAGE4_STATE)

    async def execute_stage4(self, run_id, request) -> None:
        self.execute_calls += 1
        self._runs.set_status(run_id, "completed")

    async def resume_stage4(self, run_id) -> None:
        self._runs.set_status(run_id, "completed")


class _FakeStage5Runner:
    def __init__(self, runs: _Runs, stage5_run_id: UUID, *, outcome: str) -> None:
        self._runs = runs
        self._run_id = stage5_run_id
        self.outcome = outcome  # 测试可翻转 → 模拟人工裁决
        self.execute_calls = 0
        self.resume_calls = 0
        self.captured_request = None

    def _checkpoint(self) -> dict:
        return {
            "waiting_human": {"terminal": None},
            "completed": {"terminal": STAGE5_TERMINAL_FINALIZE},
            "research_required": {
                "terminal": STAGE5_TERMINAL_RESEARCH_REQUIRED,
                "research_request_id": str(_RESEARCH_REQUEST_ID),
            },
            "failed": {"terminal": STAGE5_TERMINAL_FINALIZE},
            "cancelled": {"terminal": STAGE5_TERMINAL_CANCELLED},
        }[self.outcome]

    async def read_checkpoint_state(self, run_id) -> dict:
        return dict(self._checkpoint())

    async def execute_stage5(self, run_id, request) -> None:
        self.execute_calls += 1
        self.captured_request = request
        self._runs.set_status(run_id, _STAGE5_OUTCOME_STATUS[self.outcome])

    async def resume_stage5_for_recovery(self, run_id) -> None:
        self.resume_calls += 1
        self._runs.set_status(run_id, "completed")


class _FakeChildService:
    def __init__(self, stage4_run_id: UUID, stage5_run_id: UUID) -> None:
        self._stage4_run_id = stage4_run_id
        self._stage5_run_id = stage5_run_id
        self.stage4_requests: list = []
        self.stage5_requests: list = []

    async def ensure_stage4_child(self, orchestration_id, request) -> ChildRunResult:
        self.stage4_requests.append(request)
        return ChildRunResult(run_id=self._stage4_run_id, created=True)

    async def ensure_stage5_child(self, orchestration_id, request) -> ChildRunResult:
        self.stage5_requests.append(request)
        return ChildRunResult(run_id=self._stage5_run_id, created=True)


class _Harness:
    """fake deps + 共享 checkpointer + repo monkeypatch 的记录器。"""

    def __init__(self, *, stage5_outcome: str) -> None:
        self.runs = _Runs()
        self.stage4_run_id = uuid4()
        self.stage5_run_id = uuid4()
        self.runs.add(self.stage4_run_id, status="pending")
        self.runs.add(self.stage5_run_id, status="pending")
        self.stage4_runner = _FakeStage4Runner(self.runs, self.stage4_run_id)
        self.stage5_runner = _FakeStage5Runner(
            self.runs, self.stage5_run_id, outcome=stage5_outcome
        )
        self.child_service = _FakeChildService(self.stage4_run_id, self.stage5_run_id)
        self.deps = ResearchOrchestrationDependencies(
            sessionmaker=FakeSessionMaker(),
            plan_service=FakePlanService(),
            router=_FakeRouter(),
            preparation=_FakePreparation(),
            fulfillment=_FakeFulfillment(),
            child_service=self.child_service,
            stage4_runner=self.stage4_runner,
            synthesis_service=_FakeSynthesis(),
            stage5_runner=self.stage5_runner,
        )
        self.saver = _SharedSaver()
        # orchestration 行 + 进度 / 终态记录。
        self.orchestration = make_orchestration(
            orchestration_id=_ORCH_ID, task_id=_TASK_ID, status="pending", current_phase="planning"
        )
        self.progress: list[tuple[str, str]] = []
        self.terminal_status: str | None = None
        self.failed: dict | None = None

    async def get_orchestration(self, orchestration_id):
        return self.orchestration

    async def update_progress(self, orchestration_id, *, status, current_phase):
        self.progress.append((status, current_phase))
        return self.orchestration

    async def mark_completed(self, orchestration_id, completed_at):
        self.terminal_status = "completed"
        self.orchestration.status = "completed"
        return self.orchestration

    async def mark_failed(self, orchestration_id, completed_at, *, error_code, error_message=None):
        self.terminal_status = "failed"
        self.orchestration.status = "failed"
        self.failed = {"error_code": error_code, "error_message": error_message}
        return self.orchestration

    async def mark_cancelled(self, orchestration_id, completed_at):
        self.terminal_status = "cancelled"
        self.orchestration.status = "cancelled"
        return self.orchestration

    async def get_workflow_run(self, run_id):
        row = self.runs.get(run_id)
        if row is None:
            return None
        return SimpleNamespace(status=row["status"])

    def bind(self, monkeypatch) -> None:
        monkeypatch.setattr(ResearchOrchestrationRepository, "get_by_id", self.get_orchestration)
        monkeypatch.setattr(
            ResearchOrchestrationRepository, "update_progress", self.update_progress
        )
        monkeypatch.setattr(ResearchOrchestrationRepository, "mark_completed", self.mark_completed)
        monkeypatch.setattr(ResearchOrchestrationRepository, "mark_failed", self.mark_failed)
        monkeypatch.setattr(ResearchOrchestrationRepository, "mark_cancelled", self.mark_cancelled)
        monkeypatch.setattr(WorkflowRunRepository, "get_by_id", self.get_workflow_run)

    def runner(self) -> ResearchOrchestrationRunner:
        return ResearchOrchestrationRunner(FakeSessionMaker(), self.saver, self.deps)


# ------------------------------------------------------------------ full lifecycle


@pytest.mark.asyncio
async def test_full_lifecycle_awaits_then_continues_to_completed(monkeypatch) -> None:
    """happy path → awaiting_stage5 → 人工裁决后 continuation → completed。

    验证 spec M 真机语义：`aupdate_state(as_node=ensure_stage5_child)` 只重入
    `run_or_resume_stage5`（**不重跑 ensure_stage5_child、不重建 child、不重复
    execute_stage5**），节点重查 child 终态后 fresh 路由到 complete_orchestration。
    """
    harness = _Harness(stage5_outcome="waiting_human")
    harness.bind(monkeypatch)
    runner = harness.runner()

    # ---- 首启：跑到 awaiting_stage5 暂停 ----
    final = await runner.run_orchestration(_ORCH_ID)
    assert final["current_phase"] == OrchestrationPhase.AWAITING_STAGE5.value
    assert final["stage5_run_status"] == "waiting_human"
    assert harness.terminal_status is None  # 未到终态
    assert harness.stage5_runner.execute_calls == 1
    assert harness.stage5_runner.captured_request is not None
    assert harness.stage5_runner.captured_request.synthesis_result_id == _SYNTHESIS_RESULT_ID
    assert harness.child_service.stage5_requests == [harness.stage5_runner.captured_request]
    # orchestration 投影 waiting_human / awaiting_stage5。
    assert harness.progress[-1] == (OrchestrationStatus.WAITING_HUMAN.value, "awaiting_stage5")

    # ---- 人工裁决 child（终态 finalize）→ continuation ----
    harness.stage5_runner.outcome = "completed"
    harness.runs.set_status(harness.stage5_run_id, "completed")

    final2 = await runner.run_orchestration(_ORCH_ID)
    assert harness.terminal_status == "completed"
    assert final2["current_phase"] == OrchestrationPhase.COMPLETED.value
    # 不重建 child / 不重复 execute。
    assert harness.child_service.stage5_requests == [harness.stage5_runner.captured_request]
    assert harness.stage5_runner.execute_calls == 1
    assert harness.stage5_runner.resume_calls == 0
    # stage4 execute 也只一次。
    assert harness.stage4_runner.execute_calls == 1


@pytest.mark.asyncio
async def test_continuation_stays_awaiting_when_child_still_waiting(monkeypatch) -> None:
    """child 仍 waiting_human（如 rewrite 后再次 interrupt）→ continuation 再次
    pause 到 awaiting_stage5，不推进终态、不重复 execute（spec O rewrite 路径）。"""
    harness = _Harness(stage5_outcome="waiting_human")
    harness.bind(monkeypatch)
    runner = harness.runner()

    final = await runner.run_orchestration(_ORCH_ID)
    assert final["current_phase"] == OrchestrationPhase.AWAITING_STAGE5.value

    # child 仍是 waiting_human（人工未裁决）→ 再次 continuation 依旧 pause。
    final2 = await runner.run_orchestration(_ORCH_ID)
    assert harness.terminal_status is None
    assert final2["current_phase"] == OrchestrationPhase.AWAITING_STAGE5.value
    assert final2["stage5_run_status"] == "waiting_human"
    assert harness.stage5_runner.execute_calls == 1
    assert harness.stage5_runner.resume_calls == 0
    assert harness.child_service.stage5_requests == [harness.stage5_runner.captured_request]


@pytest.mark.asyncio
async def test_continuation_reenters_run_or_resume_stage5_node(monkeypatch) -> None:
    """验证 continuation 只重入 `run_or_resume_stage5`，而不是整个 graph 重跑。

    stage5 child 保持 completed 终态 → 第二次 run 直接 complete；stage4 / stage5
    均不重复执行。这是 `as_node=ensure_stage5_child` 重算 `next` 的关键证明。
    """
    harness = _Harness(stage5_outcome="completed")
    harness.bind(monkeypatch)
    runner = harness.runner()

    # 首启即 completed（无人工 pause）。
    final = await runner.run_orchestration(_ORCH_ID)
    assert harness.terminal_status == "completed"
    assert final["current_phase"] == OrchestrationPhase.COMPLETED.value

    # terminal orchestration → runner 拒绝续接（不重跑）。
    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await runner.run_orchestration(_ORCH_ID)
    assert harness.stage4_runner.execute_calls == 1
    assert harness.stage5_runner.execute_calls == 1


# ------------------------------------------------------------------ other routes


@pytest.mark.asyncio
async def test_research_required_persists_request_and_phase(monkeypatch) -> None:
    """Stage5 research_required → pause_for_research：只持久化 research_request_id +
    phase=research_backflow（spec P），orchestration 停在 waiting_human。"""
    harness = _Harness(stage5_outcome="research_required")
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    assert final["research_request_id"] == str(_RESEARCH_REQUEST_ID)
    assert final["current_phase"] == OrchestrationPhase.RESEARCH_BACKFLOW.value


@pytest.mark.asyncio
async def test_stage5_failed_marks_orchestration_failed(monkeypatch) -> None:
    """Stage5 child FAILED → stage5_failed：orchestration failed、phase=stage5、
    error_code=stage5_execution_failed（不吞 child 错误）。"""
    harness = _Harness(stage5_outcome="failed")
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status == "failed"
    assert harness.failed == {
        "error_code": "stage5_execution_failed",
        "error_message": "stage5 child run failed",
    }
    assert final["current_phase"] == OrchestrationPhase.STAGE5.value


@pytest.mark.asyncio
async def test_stage5_cancelled_marks_orchestration_cancelled(monkeypatch) -> None:
    """Stage5 child CANCELLED → stage5_cancelled：orchestration cancelled、phase=stage5。"""
    harness = _Harness(stage5_outcome="cancelled")
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status == "cancelled"
    assert final["current_phase"] == OrchestrationPhase.STAGE5.value
