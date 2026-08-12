"""Unit-test fakes for research orchestration（0 DB / 0 network）。

共享 `FakeSession` / `FakeSessionMaker`（repo 方法用 monkeypatch 替换，session
只当 async context manager 用）+ orchestration 行 fixture 构建器。
"""

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID

from app.db.models.research_orchestration import ResearchOrchestrationModel
from app.research_orchestration.contracts import (
    ORCHESTRATION_SCHEMA_VERSION,
    ORCHESTRATOR_NAME,
    ORCHESTRATOR_VERSION,
)


class FakeSession:
    def __init__(self) -> None:
        self.committed = False
        self.rolled_back = False
        self.added: list = []

    async def __aenter__(self) -> "FakeSession":
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        self.rolled_back = True

    def add(self, model) -> None:
        self.added.append(model)


class FakeSessionMaker:
    def __init__(self) -> None:
        self.session = FakeSession()

    def __call__(self) -> FakeSession:
        return self.session


def make_orchestration(
    *,
    orchestration_id: UUID | None = None,
    task_id: UUID | None = None,
    status: str = "pending",
    current_phase: str = "planning",
    research_plan_id: UUID | None = None,
    input_fingerprint: str | None = None,
    error_code: str | None = None,
    attempt_no: int = 1,
    retry_of_orchestration_id: UUID | None = None,
) -> ResearchOrchestrationModel:
    """构造可用于 fake repo 的 orchestration 行（不落库，值不校验 DB 约束）。"""
    return ResearchOrchestrationModel(
        orchestration_id=orchestration_id or UUID("00000000-0000-0000-0000-000000000001"),
        task_id=task_id or UUID("00000000-0000-0000-0000-000000000002"),
        research_plan_id=research_plan_id,
        attempt_no=attempt_no,
        retry_of_orchestration_id=retry_of_orchestration_id,
        orchestration_schema_version=ORCHESTRATION_SCHEMA_VERSION,
        orchestrator_name=ORCHESTRATOR_NAME,
        orchestrator_version=ORCHESTRATOR_VERSION,
        status=status,
        current_phase=current_phase,
        input_fingerprint=input_fingerprint or ("a" * 64),
        error_code=error_code,
        created_at=datetime.now(UTC),
    )


class FakePlanResult:
    def __init__(self, research_plan_id: UUID, planner_input_fingerprint: str) -> None:
        self.research_plan_id = research_plan_id
        self.planner_input_fingerprint = planner_input_fingerprint


class FakePlanService:
    def __init__(self, planner_input_fingerprint: str = "p" * 64) -> None:
        self.planner_input_fingerprint = planner_input_fingerprint
        self.calls: list[UUID] = []

    async def create_plan(self, task_id: UUID) -> FakePlanResult:
        self.calls.append(task_id)
        return FakePlanResult(
            research_plan_id=UUID("00000000-0000-0000-0000-000000000003"),
            planner_input_fingerprint=self.planner_input_fingerprint,
        )


class FakeStage4Runner:
    """fake `Stage4WorkflowRunner`：只暴露 child service 需要的 create_stage4_run。"""

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with
        self.child_binds: list = []
        self.run_id = uuid.uuid4()

    async def create_stage4_run(self, request, *, on_run_created=None):
        if self.fail_with is not None:
            raise self.fail_with
        if on_run_created is not None:
            # 模拟 runner 的事务：hook 收到 (session, run_id)，把 child link 加入
            # session；fake 记录 session.added 供断言。
            session = FakeSession()
            on_run_created(session, self.run_id)
            self.child_binds.extend(session.added)
        return SimpleNamespace(run_id=self.run_id)


class FakeStage5Runner:
    """fake `Stage5WorkflowRunner`：只暴露 child service 需要的 create_stage5_run。"""

    def __init__(self, *, fail_with: BaseException | None = None) -> None:
        self.fail_with = fail_with
        self.child_binds: list = []
        self.run_id = uuid.uuid4()

    async def create_stage5_run(self, request, *, on_run_created=None):
        if self.fail_with is not None:
            raise self.fail_with
        if on_run_created is not None:
            session = FakeSession()
            on_run_created(session, self.run_id)
            self.child_binds.extend(session.added)
        return SimpleNamespace(run_id=self.run_id)


class FakeActionStage5Runner:
    """fake Stage5 runner for `act_on_orchestration`（记录 resume_stage5_human）。"""

    def __init__(self) -> None:
        self.resumes: list[tuple] = []

    async def resume_stage5_human(self, run_id, decision, comment=None):
        self.resumes.append((run_id, decision, comment))
        return {"decision": decision}


class FakeActionOrchestrationRunner:
    """fake top-level runner for `act_on_orchestration`（记录 run_orchestration）。"""

    def __init__(self) -> None:
        self.run_calls: list[UUID] = []

    async def run_orchestration(self, orchestration_id: UUID) -> dict:
        self.run_calls.append(orchestration_id)
        return {"current_phase": "awaiting_stage5"}


class FakeExecutionManager:
    """fake `ResearchOrchestrationExecutionManager`（记录 schedule / cancel_local）。"""

    def __init__(self) -> None:
        self.scheduled: list[UUID] = []
        self.cancelled: list[UUID] = []
        self._scheduled_set: set[UUID] = set()

    def schedule(self, orchestration_id: UUID) -> bool:
        if orchestration_id in self._scheduled_set:
            return False
        self._scheduled_set.add(orchestration_id)
        self.scheduled.append(orchestration_id)
        return True

    def is_scheduled(self, orchestration_id: UUID) -> bool:
        return orchestration_id in self._scheduled_set

    async def cancel_local(self, orchestration_id: UUID) -> None:
        # 与真实 manager 一致：无 live task → no-op（不记录）。
        if orchestration_id not in self._scheduled_set:
            return
        self._scheduled_set.discard(orchestration_id)
        self.cancelled.append(orchestration_id)


class FakeRecoveryRunner:
    """fake orchestration runner（recovery 协调器只读 checkpoint + 触发 run）。"""

    def __init__(self, *, checkpoint_phase: str = "stage4") -> None:
        self.checkpoint_phase = checkpoint_phase
        self.run_calls: list[UUID] = []

    async def read_orchestration_checkpoint(self, orchestration_id: UUID) -> dict:
        return {"current_phase": self.checkpoint_phase}

    async def run_orchestration(self, orchestration_id: UUID) -> dict:
        self.run_calls.append(orchestration_id)
        return {"current_phase": self.checkpoint_phase}
