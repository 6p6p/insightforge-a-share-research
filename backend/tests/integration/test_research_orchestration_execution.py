"""Top-level orchestration execution-lifecycle 集成测试（7A.2B.2 Gate spec H）。

真实 PostgreSQL + 真实 `research_planning`（FakeResearchPlannerModel）+ 真实
`research_orchestration_runs` 表；**不跑真实 Stage4/Stage5**（fake runner 只做
projection）。被测试对象是 execution lifecycle（Gate C/E/F）：快速返回 / 首次
schedule / 重复 start / waiting_human 不自动 resume / failed 409 / retry 自动
schedule / completed O2 返回 / 并发 retry 单 O2 单 task / HTTP 不 await 整图。

Concentrated cases（spec H）：
1. **HTTP start 快速返回**：fake runner blocked 在 Event 也不会让 HTTP 等它完成；
2. 首次 start → attempt=1 + schedule once（exactly 1 background task）；
3. 重复 start active → same orchestration + 无重复 local task；
   - Case 2：active=pending 但本进程无 local task（模拟重启）→ 重新 schedule；
   - Case 3：active=running → 不重复 schedule；
4. waiting_human → 返回 active，**不自动 resume**；
5. O1 failed → start → 409 `research_orchestration_retry_required`；
6. O1 failed → retry action → O2（attempt=2）**自动 schedule**（不需第二次 start）；
7. O1 failed → O2 completed → start 返回 **O2**（不返回 O1）；
8. 并发 retry → **单 O2**（FOR UPDATE 串行化）+ **单 background task**。

全程零真实 DeepSeek / 零 live provider；fake runner 镜像真实 runner 的 DB
projection 契约（失败 → mark_failed，完成 → mark_completed）。
"""

import asyncio
import threading
import time
from datetime import UTC, datetime
from uuid import UUID

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.api.dependencies import get_research_orchestration_service
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.research_orchestration.errors import ResearchOrchestrationRetryRequired
from app.research_orchestration.execution_manager import ResearchOrchestrationExecutionManager
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.research_orchestration.service import ResearchOrchestrationService
from app.research_planning.service import ResearchPlanningService
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_research_planning_service import (
    _plan_payload,
    _seed_research_task,
)
from tests.integration.test_revision_service import _cleanup_with_revisions
from tests.integration.test_valuation_claim_service import _seed_company
from tests.research_planning.fakes import FakeResearchPlannerModel

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM research_orchestration_child_runs"))
        await session.execute(text("DELETE FROM research_orchestration_runs"))
        await session.execute(text("DELETE FROM research_plan_routes"))
        await session.execute(text("DELETE FROM research_plans"))
        await session.commit()
    await _cleanup_with_revisions(sessionmaker)


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    from app.core.config import get_settings

    manager = DatabaseManager(
        database_url=get_settings().database_url,
        echo=False,
        connect_timeout_seconds=get_settings().database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    task_id = await _seed_research_task(sessionmaker)
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
        "task_id": task_id,
    }
    await _cleanup(sessionmaker)


# ---------------------------------------------------------------- fakes / helpers


class _ScriptedRunner:
    """真实 runner 的轻量 fake：记录 run_calls；按步骤 block/fail/complete 投影。

    steps 为 ("block" | "fail" | "complete") 序列（最后一步重复）：
    - block：`await gate`（threading.Event 轮询）——模拟后台长任务，供快速返回 /
      active 状态断言；
    - fail：mark_failed + raise（镜像真实 runner `_stream` 失败投影后重抛）；
    - complete：mark_completed。
    """

    def __init__(self, sessionmaker, *steps: str) -> None:
        self._sessionmaker = sessionmaker
        self._steps = steps or ("complete",)
        self._index = 0
        self.gate = threading.Event()
        self.run_calls: list[UUID] = []

    async def run_orchestration(self, orchestration_id: UUID) -> dict:
        self.run_calls.append(orchestration_id)
        kind = self._steps[min(self._index, len(self._steps) - 1)]
        self._index += 1
        if kind == "block":
            while not self.gate.is_set():
                await asyncio.sleep(0.02)
        elif kind == "fail":
            await _mark_failed(self._sessionmaker, orchestration_id)
            raise RuntimeError("simulated execution failure")
        await _mark_completed(self._sessionmaker, orchestration_id)
        return {"current_phase": "completed"}


def _planner(sessionmaker) -> ResearchPlanningService:
    return ResearchPlanningService(
        sessionmaker,
        FakeResearchPlannerModel(payload=_plan_payload()),
        CompanyIdentityService(sessionmaker),
    )


def _make_service(sessionmaker, runner):
    manager = ResearchOrchestrationExecutionManager(runner)
    service = ResearchOrchestrationService(
        sessionmaker,
        _planner(sessionmaker),
        orchestration_runner=runner,
        execution_manager=manager,
    )
    return service, manager


async def _mark_failed(sessionmaker, orchestration_id: UUID) -> None:
    async with sessionmaker() as session:
        await ResearchOrchestrationRepository(session).mark_failed(
            orchestration_id,
            datetime.now(UTC),
            error_code="orchestration_execution_failed",
        )
        await session.commit()


async def _mark_completed(sessionmaker, orchestration_id: UUID) -> None:
    async with sessionmaker() as session:
        await ResearchOrchestrationRepository(session).mark_completed(
            orchestration_id, datetime.now(UTC)
        )
        await session.commit()


async def _set_status(sessionmaker, orchestration_id: UUID, status: str, phase: str) -> None:
    async with sessionmaker() as session:
        await ResearchOrchestrationRepository(session).update_progress(
            orchestration_id, status=status, current_phase=phase
        )
        await session.commit()


async def _get_status(sessionmaker, orchestration_id: UUID) -> str:
    async with sessionmaker() as session:
        return (
            await session.execute(
                text(
                    "SELECT status FROM research_orchestration_runs WHERE orchestration_id = :oid"
                ).bindparams(oid=orchestration_id)
            )
        ).scalar_one()


async def _count_runs(sessionmaker) -> int:
    async with sessionmaker() as session:
        rows = await session.execute(text("SELECT count(*) FROM research_orchestration_runs"))
        return int(rows.scalar_one())


async def _wait_until(predicate, *, timeout: float = 30.0, message: str) -> None:
    deadline = time.monotonic() + timeout
    while True:
        result = predicate()
        if asyncio.iscoroutine(result):
            result = await result
        if result:
            return
        if time.monotonic() > deadline:
            raise AssertionError(message)
        await asyncio.sleep(0.05)


async def _drain(manager, *oids: UUID) -> None:
    """放行所有 gate 并等待对应 local task 完成（防泄漏）。"""
    for oid in oids:
        await _wait_until(
            lambda o=oid: not manager.is_scheduled(o),
            message=f"background task {oid} should drain",
        )


# ---------------------------------------------------------------- spec H cases


async def test_start_first_schedule_once(env) -> None:
    """Case 1 / spec H.2：首次 start → attempt=1、created、scheduled、exactly 1 run。"""
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "block")
    service, manager = _make_service(sessionmaker, runner)
    try:
        outcome = await service.prepare_orchestration_start(env["task_id"])
        o1 = outcome.orchestration.orchestration_id
        assert outcome.created is True
        assert outcome.scheduled is True
        assert outcome.orchestration.attempt_no == 1
        assert outcome.orchestration.retry_of_orchestration_id is None
        assert outcome.orchestration.status == "pending"
        assert manager.is_scheduled(o1)
        # 后台 task 已调度；等它真正开始（run_orchestration 进入后记录调用）。
        await _wait_until(
            lambda: len(runner.run_calls) == 1, message="O1 background run should start"
        )
        assert runner.run_calls == [o1]  # 恰好一次 background run
    finally:
        runner.gate.set()
        await _drain(manager, outcome.orchestration.orchestration_id)
    assert await _get_status(sessionmaker, outcome.orchestration.orchestration_id) == "completed"


async def test_repeat_start_active_no_duplicate_task(env) -> None:
    """spec H.3 + Case 3：重复 start active → same orchestration + 无重复 local task。

    - 已有 live task（pending）→ scheduled=False（route 200），run_calls 不变；
    - active=running → 不重复 schedule。
    """
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "block")
    service, manager = _make_service(sessionmaker, runner)
    try:
        first = await service.prepare_orchestration_start(env["task_id"])
        o1 = first.orchestration.orchestration_id
        assert first.created is True and first.scheduled is True
        assert manager.is_scheduled(o1)

        # 重复 start（pending + 已有 live task）→ exact active，无重复 schedule。
        second = await service.prepare_orchestration_start(env["task_id"])
        assert second.created is False
        assert second.scheduled is False
        assert second.orchestration.orchestration_id == o1
        assert second.orchestration.attempt_no == 1
        assert runner.run_calls == [o1]
        assert await _count_runs(sessionmaker) == 1

        # Case 3：active=running → 不重复 schedule（run_calls 不变）。
        await _set_status(sessionmaker, o1, "running", "preparing")
        third = await service.prepare_orchestration_start(env["task_id"])
        assert third.orchestration.orchestration_id == o1
        assert third.created is False
        assert third.scheduled is False
        assert runner.run_calls == [o1]
    finally:
        runner.gate.set()
        await _drain(manager, first.orchestration.orchestration_id)


async def test_repeat_start_pending_reschedules_after_restart(env) -> None:
    """Case 2：active=pending 但本进程无 local task（模拟重启）→ 重新 schedule。"""
    sessionmaker = env["sessionmaker"]
    runner_a = _ScriptedRunner(sessionmaker, "block")
    service_a, manager_a = _make_service(sessionmaker, runner_a)
    o1: UUID | None = None
    runner_b = None
    manager_b = None
    try:
        first = await service_a.prepare_orchestration_start(env["task_id"])
        o1 = first.orchestration.orchestration_id
        assert first.scheduled is True
        assert manager_a.is_scheduled(o1)

        # 模拟进程重启：本地 task 取消（registry 清除），DB 行保持 pending。
        await manager_a.cancel_local(o1)
        assert not manager_a.is_scheduled(o1)
        assert await _get_status(sessionmaker, o1) == "pending"

        # 重启后新 manager：重新调度同一 active（不新建 orchestration）。
        runner_b = _ScriptedRunner(sessionmaker, "block")
        service_b, manager_b = _make_service(sessionmaker, runner_b)
        resumed = await service_b.prepare_orchestration_start(env["task_id"])
        assert resumed.orchestration.orchestration_id == o1
        assert resumed.created is False
        assert resumed.scheduled is True
        await _wait_until(
            lambda: len(runner_b.run_calls) >= 1, message="resumed O1 run should start"
        )
        assert runner_b.run_calls == [o1]
        assert await _count_runs(sessionmaker) == 1
    finally:
        runner_a.gate.set()
        if o1 is not None:
            await _drain(manager_a, o1)
        if runner_b is not None:
            runner_b.gate.set()
            await _drain(manager_b, o1)


async def test_start_waiting_human_no_auto_resume(env) -> None:
    """spec H.4 / Case 4：waiting_human → 返回 active，不自动 resume。"""
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "block")
    service, manager = _make_service(sessionmaker, runner)
    try:
        outcome = await service.prepare_orchestration_start(env["task_id"])
        o1 = outcome.orchestration.orchestration_id
        assert outcome.scheduled is True

        # runner 仍在 block（未投影），直接置 waiting_human → start 不自动 resume。
        await _set_status(sessionmaker, o1, "waiting_human", "awaiting_stage5")
        outcome2 = await service.prepare_orchestration_start(env["task_id"])
        assert outcome2.orchestration.orchestration_id == o1
        assert outcome2.created is False
        assert outcome2.scheduled is False  # waiting_human 不调度
        assert runner.run_calls == [o1]  # 无 resume run
    finally:
        runner.gate.set()
        await _drain(manager, outcome.orchestration.orchestration_id)


async def test_start_failed_latest_raises_retry_required(env) -> None:
    """spec H.5 / Case 6：O1 failed → start → 409，不偷偷回到 attempt1。"""
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "complete")
    service, manager = _make_service(sessionmaker, runner)
    try:
        outcome = await service.prepare_orchestration_start(env["task_id"])
        o1 = outcome.orchestration.orchestration_id
        await _drain(manager, o1)
        assert await _get_status(sessionmaker, o1) == "completed"

        await _mark_failed(sessionmaker, o1)  # 模拟 O1 真实失败（repo 投影等价）。
        with pytest.raises(ResearchOrchestrationRetryRequired):
            await service.prepare_orchestration_start(env["task_id"])
        assert await _count_runs(sessionmaker) == 1  # 未新建 O2 / 未回 attempt1
        assert runner.run_calls == [o1]
    finally:
        runner.gate.set()


async def test_retry_and_schedule_auto_schedules_o2(env) -> None:
    """spec H.6 / Gate E：O1 failed → retry → O2 attempt2 **自动后台调度**。

    retry action 本身即触发 schedule（不需第二次 API start）。
    """
    sessionmaker = env["sessionmaker"]
    # O1 完成步 + O2 阻塞步：断言 O2 被 auto-schedule 时其 task 仍在 registry。
    runner = _ScriptedRunner(sessionmaker, "complete", "block")
    service, manager = _make_service(sessionmaker, runner)
    o2_id: UUID | None = None
    try:
        outcome = await service.prepare_orchestration_start(env["task_id"])
        o1 = outcome.orchestration.orchestration_id
        await _drain(manager, o1)
        assert runner.run_calls == [o1]
        await _mark_failed(sessionmaker, o1)

        o2 = await service.retry_and_schedule(o1)
        o2_id = o2.orchestration_id
        assert o2_id != o1
        assert o2.attempt_no == 2
        assert o2.retry_of_orchestration_id == o1
        assert o2.status == "pending"
        assert manager.is_scheduled(o2_id)  # 自动调度，无需第二次 API 调用
        # retry 本身触发了一次 O2 run（后台，不 await 完成）。
        await _wait_until(
            lambda: o2_id in runner.run_calls, message="O2 background run should start"
        )
        assert runner.run_calls == [o1, o2_id]
    finally:
        runner.gate.set()
        await _drain(manager, outcome.orchestration.orchestration_id, o2_id)
    assert await _get_status(sessionmaker, o2_id) == "completed"


async def test_start_returns_completed_o2_not_o1(env) -> None:
    """spec H.7 / Case 5：O1 failed → O2 completed → start 返回 O2（不返回 O1）。"""
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "complete")
    service, manager = _make_service(sessionmaker, runner)
    try:
        outcome = await service.prepare_orchestration_start(env["task_id"])
        o1 = outcome.orchestration.orchestration_id
        await _drain(manager, o1)
        await _mark_failed(sessionmaker, o1)

        o2 = await service.retry_and_schedule(o1)
        o2_id = o2.orchestration_id
        await _drain(manager, o2_id)
        assert await _get_status(sessionmaker, o2_id) == "completed"

        # 无 active；latest=O2（created_at 晚）→ 返回 O2，不返回 attempt1 O1。
        final = await service.prepare_orchestration_start(env["task_id"])
        assert final.created is False
        assert final.scheduled is False
        assert final.orchestration.orchestration_id == o2_id
        assert final.orchestration.attempt_no == 2
        assert final.orchestration.status == "completed"
    finally:
        runner.gate.set()


async def test_concurrent_retry_single_o2_single_task(env) -> None:
    """spec H.8：并发 retry → 单 O2（FOR UPDATE 串行化）+ 单 background task。

    O2 的 run 用 "block" 步：在并发窗口内保持 live，兄弟协程的 schedule 因
    `manager` 同 id 单 live task 语义返回 False → 只触发一次 run。
    """
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "complete", "block")
    service, manager = _make_service(sessionmaker, runner)
    o2_id: UUID | None = None
    try:
        outcome = await service.prepare_orchestration_start(env["task_id"])
        o1 = outcome.orchestration.orchestration_id
        await _drain(manager, o1)
        await _mark_failed(sessionmaker, o1)

        results = await asyncio.gather(
            service.retry_and_schedule(o1),
            service.retry_and_schedule(o1),
            service.retry_and_schedule(o1),
        )
        o2_id = results[0].orchestration_id
        assert len({r.orchestration_id for r in results}) == 1  # 单 O2
        assert all(r.attempt_no == 2 for r in results)
        assert all(r.retry_of_orchestration_id == o1 for r in results)
        assert await _count_runs(sessionmaker) == 2  # O1 + 仅一个 O2

        # O2 的 run 只被触发一次（manager 同 id 单 live task）。
        await _wait_until(
            lambda: len(runner.run_calls) >= 2, message="O2 background run should start"
        )
        assert runner.run_calls.count(o2_id) == 1  # 单 background task
    finally:
        runner.gate.set()
        await _drain(manager, outcome.orchestration.orchestration_id, o2_id)


async def test_http_start_returns_before_blocked_runner_completes(app, env) -> None:
    """spec H.1 / Gate C：HTTP start 快速返回，不 await 整个 LangGraph。

    fake runner 永久 block 在 Event 上——若 handler await 整个研究，HTTP 请求
    永远不返回。这里 POST 立即返回 201，且本地后台 task 已调度。
    """
    sessionmaker = env["sessionmaker"]
    runner = _ScriptedRunner(sessionmaker, "block")
    service, manager = _make_service(sessionmaker, runner)
    app.dependency_overrides[get_research_orchestration_service] = lambda: service
    oid: UUID | None = None
    with TestClient(app) as client:
        started_at = time.monotonic()
        response = client.post(f"/api/v1/tasks/{env['task_id']}/orchestrations")
        elapsed = time.monotonic() - started_at
        assert response.status_code == 201
        assert elapsed < 5.0  # runner 阻塞，HTTP 仍立即返回
        body = response.json()
        oid = UUID(body["orchestration_id"])
        assert body["status"] == "pending"
        assert manager.is_scheduled(oid)
        assert runner.run_calls == [oid]
        runner.gate.set()
        await _drain(manager, oid)
    assert await _get_status(sessionmaker, oid) == "completed"
