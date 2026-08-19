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

from app.audit.errors import (
    ReportAuditModelUnavailable,
    ReportAuditValidationError,
)
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.research_orchestration.contracts import (
    RESUME_KIND_PREPARE,
    RESUME_KIND_STAGE5_RETRY,
    RESUME_KIND_SUPPLEMENTAL_RESEARCH,
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.errors import (
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationInvalidAction,
)
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
    def __init__(
        self, runs: _Runs, stage5_run_id: UUID, *, outcome: str, raise_on_execute=None
    ) -> None:
        self._runs = runs
        self._run_id = stage5_run_id
        self.outcome = outcome  # 测试可翻转 → 模拟人工裁决
        # P0：模拟 audit 终态失败（execute 时抛并给 child run 打 failed，镜像
        # 生产 stage5 runner `_mark_failed` 后重抛）。
        self.raise_on_execute = raise_on_execute
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
        if self.raise_on_execute is not None:
            self._runs.set_status(run_id, "failed")
            raise self.raise_on_execute
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
        self.stage4_attempts: list[int] = []
        self.stage5_attempts: list[int] = []
        self.stage4_sources: list = []
        self.stage5_sources: list = []

    async def ensure_stage4_child(
        self, orchestration_id, request, *, attempt_no=1, source_research_request_id=None
    ) -> ChildRunResult:
        self.stage4_requests.append(request)
        self.stage4_attempts.append(attempt_no)
        self.stage4_sources.append(source_research_request_id)
        return ChildRunResult(run_id=self._stage4_run_id, created=True)

    async def ensure_stage5_child(
        self, orchestration_id, request, *, attempt_no=1, source_research_request_id=None
    ) -> ChildRunResult:
        self.stage5_requests.append(request)
        self.stage5_attempts.append(attempt_no)
        self.stage5_sources.append(source_research_request_id)
        return ChildRunResult(run_id=self._stage5_run_id, created=True)


class _FakeBackflowService:
    """backflow 节点的最小 fake：create_or_get_plan / verify request / fulfill /
    build continuation request 全部确定性返回。

    `create_or_get_plan` 幂等 replay（7A.2B.3 spec：同 research_request_id → 同
    plan id，**不新建 SupplementalPlan**）——K2 resume 断言据此。
    """

    def __init__(self, plan_payload: dict | None = None) -> None:
        self.plan_requests: list = []
        self.fulfillments: list = []
        self.plan_ids: list[UUID] = []
        self._plan_ids: dict[UUID, UUID] = {}
        self.plan_payload = plan_payload if plan_payload is not None else {"need_specs": []}

    async def create_or_get_plan(self, research_backflow_request_id):
        self.plan_requests.append(research_backflow_request_id)
        plan_id = self._plan_ids.setdefault(research_backflow_request_id, uuid4())
        self.plan_ids.append(plan_id)
        return SimpleNamespace(backflow_plan_id=plan_id, plan_payload=self.plan_payload)

    async def verify_research_request_integrity(self, research_request_id):
        return SimpleNamespace()  # fake executor 不读字段

    async def fulfill_request(self, research_request_id, new_synthesis_result_id):
        self.fulfillments.append((research_request_id, new_synthesis_result_id))
        return SimpleNamespace(fulfillment_id=uuid4())

    async def build_stage5_continuation_request(self, fulfillment_id):
        return SimpleNamespace(
            task_id=_TASK_ID,
            company_id=_COMPANY_ID,
            research_question="分析目标公司基本面",
            analysis_as_of="2026-06-30",
            synthesis_result_id=_SYNTHESIS_RESULT_ID,
        )


class _FakeBackflowExecutor:
    """fake executor：返回配置的"新增"证据卡 id + per-need manual reasons。

    attempts 投影 `manual_required_reason`（7A Product Gate spec I）——executor
    级 reasons 进 `backflow_executor_manual_reasons` state key；不配置 → attempts
    空（既有 no_progress / structured 路径不变）。
    """

    def __init__(
        self,
        *,
        new_card_ids: tuple[str, ...] = (),
        manual_required_reasons: tuple[str, ...] = (),
    ) -> None:
        self._new_card_ids = new_card_ids
        self._manual_required_reasons = manual_required_reasons
        self.calls = 0

    async def execute_supplemental_research(self, verified_request, plan_payload):
        self.calls += 1
        return SimpleNamespace(
            new_evidence_card_ids=tuple(UUID(c) for c in self._new_card_ids),
            attempts=tuple(
                SimpleNamespace(
                    need_code=f"need_{i}",
                    status="manual_required",
                    created_evidence_card_ids=(),
                    replayed_evidence_card_ids=(),
                    manual_required_reason=reason,
                )
                for i, reason in enumerate(self._manual_required_reasons)
            ),
        )


class _Harness:
    """fake deps + 共享 checkpointer + repo monkeypatch 的记录器。"""

    def __init__(
        self,
        *,
        stage5_outcome: str,
        backflow_new_cards: tuple[str, ...] = (),
        backflow_plan_payload: dict | None = None,
        preparation_ready: bool = True,
        backflow_executor_manual_reasons: tuple[str, ...] = (),
    ) -> None:
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
        self.backflow_service = _FakeBackflowService(plan_payload=backflow_plan_payload)
        self.backflow_executor = _FakeBackflowExecutor(
            new_card_ids=backflow_new_cards,
            manual_required_reasons=backflow_executor_manual_reasons,
        )
        self.preparation = _FakePreparation(ready=preparation_ready)
        self.deps = ResearchOrchestrationDependencies(
            sessionmaker=FakeSessionMaker(),
            plan_service=FakePlanService(),
            router=_FakeRouter(),
            preparation=self.preparation,
            fulfillment=_FakeFulfillment(),
            child_service=self.child_service,
            stage4_runner=self.stage4_runner,
            synthesis_service=_FakeSynthesis(),
            stage5_runner=self.stage5_runner,
            backflow_service=self.backflow_service,
            backflow_executor=self.backflow_executor,
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
async def test_research_required_backflow_loop_to_limit(monkeypatch) -> None:
    """research_required → backflow loop（每轮新增 EvidenceCard）→ 两轮后达
    MAX_BACKFLOW_RESEARCH_ROUNDS → research_backflow_manual（limit_reached）。

    验证：round 递增、Stage4/5 child attempt 1/2/3、每轮 execute_supplemental_
    research、backflow child link 记录 source_research_request_id、最终
    waiting_human + phase=research_backflow + 稳定 reason。
    """
    harness = _Harness(
        stage5_outcome="research_required",
        backflow_new_cards=("00000000-0000-0000-0000-0000000000a1",),
    )
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    assert final["research_request_id"] == str(_RESEARCH_REQUEST_ID)
    assert final["current_phase"] == OrchestrationPhase.RESEARCH_BACKFLOW.value
    assert final["backflow_manual_reason"] == "research_backflow_limit_reached"
    assert final["backflow_round"] == 2
    # 首启 attempt1 + 两轮 backflow attempt2/3。
    assert harness.child_service.stage4_attempts == [1, 2, 3]
    assert harness.child_service.stage5_attempts == [1, 2, 3]
    assert harness.backflow_executor.calls == 2
    # backflow child link 记录 source_research_request_id（attempt1 为 None）。
    assert harness.child_service.stage4_sources[1:] == [_RESEARCH_REQUEST_ID] * 2
    assert harness.child_service.stage5_sources[1:] == [_RESEARCH_REQUEST_ID] * 2
    # fake run 复用同一 run：execute 只发生在首启（attempt2/3 复用 completed run）。
    assert harness.stage5_runner.execute_calls == 1


@pytest.mark.asyncio
async def test_research_required_no_progress_manual(monkeypatch) -> None:
    """research_required → execute 无新增 EvidenceCard → verify_progress no_progress
    → research_backflow_manual（reason=research_backflow_no_progress），不进入
    Stage4/Stage5 backflow attempt。"""
    harness = _Harness(stage5_outcome="research_required", backflow_new_cards=())
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    assert final["backflow_manual_reason"] == "research_backflow_no_progress"
    assert final["backflow_round"] == 1
    assert harness.backflow_executor.calls == 1
    # 无进度 → 不创建 Stage4/Stage5 backflow attempt。
    assert harness.child_service.stage4_attempts == [1]
    assert harness.child_service.stage5_attempts == [1]


@pytest.mark.asyncio
async def test_research_required_structured_manual_reason(monkeypatch) -> None:
    """7A.2B.3 scope 冻结：plan 投影 structured 需求（manual_required_reasons）且
    execute 无新增证据卡 → verify_progress 给稳定 reason
    structured_data_refresh_required，**不误报 research_backflow_no_progress**。"""
    harness = _Harness(
        stage5_outcome="research_required",
        backflow_new_cards=(),
        backflow_plan_payload={
            "need_specs": [],
            "max_queries_per_need": 3,
            "manual_required_reasons": ["structured_data_refresh_required"],
        },
    )
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    assert final["backflow_manual_reason"] == "structured_data_refresh_required"
    assert final["backflow_round"] == 1
    assert harness.backflow_executor.calls == 1
    # structured manual → 不进入 Stage4/Stage5 backflow attempt（与 no_progress 同路径）。
    assert harness.child_service.stage4_attempts == [1]
    assert harness.child_service.stage5_attempts == [1]


@pytest.mark.asyncio
async def test_mixed_document_progress_and_structured_manual_reason(monkeypatch) -> None:
    """7A Product Gate spec C3：plan 同时含 document 自动 need + structured manual
    need，executor 产出新 Document EvidenceCard（has_progress=True）→ **仍必须**
    waiting_human manual_reason=structured_data_refresh_required，**不得**进入
    Stage4 next attempt / fulfillment / Stage5 continuation（verify_progress 的
    plan 级 manual_reasons 恒常优先，不随 has_progress 翻转）。"""
    harness = _Harness(
        stage5_outcome="research_required",
        backflow_new_cards=("00000000-0000-0000-0000-0000000000a1",),
        backflow_plan_payload={
            "need_specs": [
                {
                    "need_code": "unsupported_by_evidence",
                    "target_section_ids": [],
                    "related_claim_ids": [],
                    "related_evidence_card_ids": [],
                    "retrieval_queries": ["x"],
                    "allowed_source_types": ["annual_report"],
                }
            ],
            "max_queries_per_need": 3,
            "manual_required_reasons": ["structured_data_refresh_required"],
        },
    )
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    # structured 恒常优先：即使有新 Document 证据卡，reason 仍是 structured。
    assert final["backflow_manual_reason"] == "structured_data_refresh_required"
    assert final["backflow_round"] == 1
    assert harness.backflow_executor.calls == 1
    # 0 新增 fulfillment、0 Stage4/Stage5 backflow attempt（attempt 只有首启 1）。
    assert harness.backflow_service.fulfillments == []
    assert harness.child_service.stage4_attempts == [1]
    assert harness.child_service.stage5_attempts == [1]


@pytest.mark.asyncio
async def test_executor_source_acquisition_reason_propagates(monkeypatch) -> None:
    """7A Product Gate spec I：executor 级 manual reason（缺 eligible source →
    source_acquisition_required）投影进 `backflow_executor_manual_reasons`，
    verify_progress 给稳定 reason source_acquisition_required（**不误报
    research_backflow_no_progress**）→ resume API 可依此分类 K2。"""
    harness = _Harness(
        stage5_outcome="research_required",
        backflow_new_cards=(),
        backflow_executor_manual_reasons=("source_acquisition_required",),
    )
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    assert final["backflow_manual_reason"] == "source_acquisition_required"
    assert final["backflow_round"] == 1
    assert harness.backflow_executor.calls == 1
    # 无新增卡 → 不进入 Stage4/Stage5 backflow attempt（与 no_progress 同路径）。
    assert harness.child_service.stage4_attempts == [1]
    assert harness.child_service.stage5_attempts == [1]


@pytest.mark.asyncio
async def test_k1_resume_waiting_manual_to_stage4(monkeypatch) -> None:
    """K1（spec J）：waiting_manual（补资料前 not ready）→ 补资料后
    resume_after_source_acquisition(kind=prepare) → prepare **重算** readiness →
    ready → Stage4 attempt 1 → Stage5 → awaiting_stage5 暂停。同 thread 同
    orchestration，不消耗 backflow round。"""
    harness = _Harness(stage5_outcome="waiting_human", preparation_ready=False)
    harness.bind(monkeypatch)
    runner = harness.runner()

    # 首启：prepare 不 ready → fulfill → prepare_again 仍 not ready → waiting_manual END。
    final = await runner.run_orchestration(_ORCH_ID)
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.WAITING_MANUAL.value,
    )
    assert final["current_phase"] == OrchestrationPhase.WAITING_MANUAL.value
    assert harness.child_service.stage4_attempts == []
    assert harness.child_service.stage5_attempts == []

    # 补资料后 prepare ready → resume（kind=prepare）。
    harness.preparation.ready = True
    final2 = await runner.resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_PREPARE)

    assert harness.terminal_status is None
    assert final2["current_phase"] == OrchestrationPhase.AWAITING_STAGE5.value
    assert final2["stage5_run_status"] == "waiting_human"
    # 重路由 → Stage4 attempt 1 → Stage5 attempt 1（不重建、不重复）。
    assert harness.child_service.stage4_attempts == [1]
    assert harness.child_service.stage5_attempts == [1]
    assert harness.stage4_runner.execute_calls == 1
    assert harness.stage5_runner.execute_calls == 1
    # 无 backflow：round 从未设置。
    assert final2.get("backflow_round") is None


@pytest.mark.asyncio
async def test_k1_resume_still_not_ready_stays_waiting_manual(monkeypatch) -> None:
    """K1 仍缺资料：resume(kind=prepare) 后 prepare 仍 not ready → 再次
    waiting_manual END（不创建任何 child、不进入 Stage4）。"""
    harness = _Harness(stage5_outcome="waiting_human", preparation_ready=False)
    harness.bind(monkeypatch)
    runner = harness.runner()

    final = await runner.run_orchestration(_ORCH_ID)
    assert final["current_phase"] == OrchestrationPhase.WAITING_MANUAL.value

    # 仍未补足资料 → resume 后仍 waiting_manual。
    final2 = await runner.resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_PREPARE)
    assert harness.terminal_status is None
    assert final2["current_phase"] == OrchestrationPhase.WAITING_MANUAL.value
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.WAITING_MANUAL.value,
    )
    assert harness.child_service.stage4_attempts == []
    assert harness.child_service.stage5_attempts == []
    assert harness.stage4_runner.execute_calls == 0


@pytest.mark.asyncio
async def test_k2_resume_same_round_no_progress(monkeypatch) -> None:
    """K2（spec J/K2）：research_backflow + reason=source_acquisition_required →
    resume(kind=supplemental_research) 同 research_request_id + 同 backflow_round
    重跑 execute_supplemental_research（不 round+1、不新建 plan）。仍无新增卡 →
    再次 research_backflow_manual，round 保持 1。"""
    harness = _Harness(
        stage5_outcome="research_required",
        backflow_new_cards=(),
        backflow_executor_manual_reasons=("source_acquisition_required",),
    )
    harness.bind(monkeypatch)
    runner = harness.runner()

    final = await runner.run_orchestration(_ORCH_ID)
    assert final["backflow_manual_reason"] == "source_acquisition_required"
    assert final["backflow_round"] == 1

    # 补资料后重跑（executor 仍无新增卡 → 再 manual_required）。
    final2 = await runner.resume_after_source_acquisition(
        _ORCH_ID, RESUME_KIND_SUPPLEMENTAL_RESEARCH
    )
    assert harness.terminal_status is None
    assert final2["backflow_round"] == 1  # 同 round，不 round+1
    assert final2["backflow_manual_reason"] == "source_acquisition_required"
    assert harness.backflow_executor.calls == 2
    assert harness.child_service.stage4_attempts == [1]
    assert harness.child_service.stage5_attempts == [1]
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    # create_or_get_plan replay：两次执行同一 plan id（不新建 SupplementalPlan）。
    assert len(set(harness.backflow_service.plan_ids)) == 1


@pytest.mark.asyncio
async def test_k2_resume_progress_consumes_same_plan(monkeypatch) -> None:
    """K2 progress：resume 后 executor 产出新增卡 → verify_progress 有进度 →
    prepare_updated_analysis 创建 Stage4 attempt 2（同 round 内消费新增证据）→
    继续正常 backflow loop。plan replay 不新建（同一 plan id），最终仍受 MAX
    rounds 限制落到 limit_reached（K3 语义保留，不绕过）。"""
    harness = _Harness(
        stage5_outcome="research_required",
        backflow_new_cards=(),
        backflow_executor_manual_reasons=("source_acquisition_required",),
    )
    harness.bind(monkeypatch)
    runner = harness.runner()

    final = await runner.run_orchestration(_ORCH_ID)
    assert final["backflow_manual_reason"] == "source_acquisition_required"
    assert final["backflow_round"] == 1

    # 补资料 → executor 开始产出新增证据卡（manual reason 清空）。
    harness.backflow_executor._new_card_ids = ("00000000-0000-0000-0000-0000000000b1",)
    harness.backflow_executor._manual_required_reasons = ()

    final2 = await runner.resume_after_source_acquisition(
        _ORCH_ID, RESUME_KIND_SUPPLEMENTAL_RESEARCH
    )
    # 有进度 → Stage4 attempt 2 → Stage5 attempt 2 又 research_required → round 2 →
    # 继续 → 达 MAX → limit_reached END（K3 不绕过）。
    assert final2["backflow_manual_reason"] == "research_backflow_limit_reached"
    assert final2["backflow_round"] == 2
    assert harness.child_service.stage4_attempts == [1, 2, 3]
    assert harness.child_service.stage5_attempts == [1, 2, 3]
    assert harness.backflow_executor.calls == 3  # 首启 + resume + round2
    # create_or_get_plan replay 幂等：全部同一 plan id（不新建 SupplementalPlan）。
    assert len(set(harness.backflow_service.plan_ids)) == 1


@pytest.mark.asyncio
async def test_resume_terminal_rejected(monkeypatch) -> None:
    """terminal orchestration → resume 拒绝（AlreadyFinished），与 run 一致。"""
    harness = _Harness(stage5_outcome="completed")
    harness.bind(monkeypatch)
    runner = harness.runner()

    await runner.run_orchestration(_ORCH_ID)
    assert harness.terminal_status == "completed"

    with pytest.raises(ResearchOrchestrationAlreadyFinished):
        await runner.resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_PREPARE)


@pytest.mark.asyncio
async def test_resume_unsupported_kind_rejected(monkeypatch) -> None:
    """未知 resume kind → InvalidAction（runner 层防御；正常 kind 由 service 分类）。"""
    harness = _Harness(stage5_outcome="waiting_human", preparation_ready=False)
    harness.bind(monkeypatch)
    runner = harness.runner()

    final = await runner.run_orchestration(_ORCH_ID)
    assert final["current_phase"] == OrchestrationPhase.WAITING_MANUAL.value

    with pytest.raises(ResearchOrchestrationInvalidAction):
        await runner.resume_after_source_acquisition(_ORCH_ID, "bogus_kind")


@pytest.mark.asyncio
async def test_resume_without_checkpoint_rejected(monkeypatch) -> None:
    """无顶层 checkpoint（从未跑过）→ resume 拒绝（InvalidAction，无可续接）。"""
    harness = _Harness(stage5_outcome="waiting_human")
    harness.bind(monkeypatch)
    with pytest.raises(ResearchOrchestrationInvalidAction):
        await harness.runner().resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_PREPARE)


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

@pytest.mark.asyncio
async def test_stage5_audit_validation_degraded_routes_to_human_closure(monkeypatch) -> None:
    """P0 degradation：Stage5 child 执行中 audit 创建失败（有界纠正重试耗尽 →
    ReportAuditValidationError；report+check 已生成）→ **不**把 orchestration 打
    成 failed：路由 research_backflow_manual 人工闭环（reason=report_audit_unavailable，
    前端三按钮；接受被确定性拒绝，因无 audit 记录）。"""
    harness = _Harness(stage5_outcome="waiting_human")
    harness.stage5_runner.raise_on_execute = ReportAuditValidationError("issue E12 未绑定")
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert harness.failed is None
    assert harness.progress[-1] == (
        OrchestrationStatus.WAITING_HUMAN.value,
        OrchestrationPhase.RESEARCH_BACKFLOW.value,
    )
    assert final["stage5_run_status"] == "audit_degraded"
    assert final["backflow_manual_reason"] == "report_audit_unavailable"
    assert final["current_phase"] == OrchestrationPhase.RESEARCH_BACKFLOW.value
    assert harness.stage5_runner.execute_calls == 1
    assert harness.child_service.stage5_attempts == [1]


@pytest.mark.asyncio
async def test_stage5_audit_model_unavailable_degraded_reason(monkeypatch) -> None:
    """P0：模型不可用（ReportAuditModelUnavailable，重试耗尽）→ 同一降级闭环，
    reason=report_audit_model_unavailable（前端可区分）。"""
    harness = _Harness(stage5_outcome="waiting_human")
    harness.stage5_runner.raise_on_execute = ReportAuditModelUnavailable()
    harness.bind(monkeypatch)
    final = await harness.runner().run_orchestration(_ORCH_ID)

    assert harness.terminal_status is None
    assert final["stage5_run_status"] == "audit_degraded"
    assert final["backflow_manual_reason"] == "report_audit_model_unavailable"


@pytest.mark.asyncio
async def test_stage5_degraded_retry_resumes_new_attempt_then_completes(monkeypatch) -> None:
    """P0：audit-degraded 后人工"再次补充研究"（RESUME_KIND_STAGE5_RETRY）→ 新
    Stage5 attempt（retry_count+1）重跑；本次 audit 成功 → 正常 complete。"""
    harness = _Harness(stage5_outcome="failed")
    harness.stage5_runner.raise_on_execute = ReportAuditValidationError("bad ref")
    harness.bind(monkeypatch)
    runner = harness.runner()
    final = await runner.run_orchestration(_ORCH_ID)
    assert final["backflow_manual_reason"] == "report_audit_unavailable"

    # 人工再次补充研究 → 重跑 Stage5（attempt 2）；本次成功完成。
    harness.stage5_runner.raise_on_execute = None
    harness.stage5_runner.outcome = "completed"
    harness.runs.set_status(harness.stage5_run_id, "pending")
    final2 = await runner.resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_STAGE5_RETRY)

    assert harness.terminal_status == "completed"
    assert harness.child_service.stage5_attempts == [1, 2]
    assert final2.get("stage5_retry_count") == 1
    assert harness.stage5_runner.execute_calls == 2


@pytest.mark.asyncio
async def test_stage5_degraded_retry_bounded_cap(monkeypatch) -> None:
    """P0：STAGE5_RETRY 有界（MAX_STAGE5_DEGRADED_RETRY_ROUNDS=3）——第 4 次
    再次补充研究被 InvalidAction 稳定拒绝，不产生无限循环。"""
    harness = _Harness(stage5_outcome="failed")
    harness.stage5_runner.raise_on_execute = ReportAuditValidationError("bad")
    harness.bind(monkeypatch)
    runner = harness.runner()
    await runner.run_orchestration(_ORCH_ID)

    for attempt in (2, 3, 4):
        harness.runs.set_status(harness.stage5_run_id, "pending")
        final = await runner.resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_STAGE5_RETRY)
        assert harness.child_service.stage5_attempts[-1] == attempt
        assert final.get("stage5_retry_count") == attempt - 1
        assert harness.terminal_status is None
        assert final["backflow_manual_reason"] == "report_audit_unavailable"

    harness.runs.set_status(harness.stage5_run_id, "pending")
    with pytest.raises(ResearchOrchestrationInvalidAction):
        await runner.resume_after_source_acquisition(_ORCH_ID, RESUME_KIND_STAGE5_RETRY)
