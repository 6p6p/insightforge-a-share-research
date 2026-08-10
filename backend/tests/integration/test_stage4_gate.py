"""Stage 4 Final Gate tests (spec O): ResearchTask → WorkflowRun linkage.

真实 PostgreSQL + 零 LLM。专门覆盖 Final Gate 验收点：

- Stage4 run 必须绑定 task_id（create 后 DB 行 task_id 落库）；
- task 缺失 → `Stage4ResearchTaskNotFound`（不猜任务 / 不自动创建 fake
  ResearchTask），且不产生任何 run 行；
- 同一 task 并发创建两个 active run → 仅一个成功，另一个
  `ActiveWorkflowRunExists`（partial unique index 兜底）；
- Stage 1 用户 retry 语义：completed 后再次 create_simulation_run →
  **新 run / 新 thread**；
- Stage 4 internal recovery 语义：`claim_failed_for_recovery` 复用**同
  run / 同 thread**（FAILED → RUNNING 原地转，不产生新 run 行）。
"""

import asyncio
from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.errors import ActiveWorkflowRunExists
from app.core.runtime import configure_asyncio_runtime
from app.db.models.research_task import ResearchTaskModel
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.errors import Stage4ResearchTaskNotFound
from app.stage4.graph import STAGE4_GRAPH_NAME, STAGE4_GRAPH_VERSION
from app.stage4.runner import Stage4WorkflowRunner
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.runner import WorkflowRunner

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_QUESTION = "贵州茅台2026年营收与估值是否合理？"
_AS_OF = date(2026, 8, 10)


# ---------------------------------------------------------------- cleanup / env


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM workflow_events"))
        await session.execute(text("DELETE FROM workflow_runs"))
        await session.execute(text("DELETE FROM research_tasks"))
        await session.execute(text("DELETE FROM report_outlines"))
        await session.execute(text("DELETE FROM claim_synthesis_results"))
        await session.execute(text("DELETE FROM claim_synthesis_runs"))
        await session.commit()


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def connection_uri() -> str:
    return to_postgres_connection_uri(get_settings().database_url)


@pytest_asyncio.fixture
async def env(sessionmaker) -> dict:
    await _cleanup(sessionmaker)
    task_id = uuid4()
    async with sessionmaker() as session:
        await ResearchTaskRepository(session).create(
            ResearchTaskModel(
                task_id=task_id,
                company_query="600519",
                research_start_date=date(2023, 1, 1),
                research_end_date=date(2026, 12, 31),
                modules=["company_profile"],
                questions=[],
                require_plan_approval=False,
            )
        )
        await session.commit()
    yield {"sessionmaker": sessionmaker, "task_id": task_id}
    await _cleanup(sessionmaker)


def _request(task_id: UUID) -> Stage4WorkflowRequest:
    """最小合法 Stage4 请求：只用于 create run（不执行 graph）。"""
    return Stage4WorkflowRequest(
        task_id=task_id,
        company_id=uuid4(),
        research_question=_QUESTION,
        analysis_as_of=_AS_OF,
        analysis_work_items=[
            {"item_id": "biz", "analysis_type": "business", "evidence_card_ids": [uuid4()]}
        ],
    )


async def _run_count(sessionmaker, task_id: UUID) -> int:
    async with sessionmaker() as session:
        return int(
            (
                await session.execute(
                    text("SELECT count(*) FROM workflow_runs WHERE task_id = :tid").bindparams(
                        tid=task_id
                    )
                )
            ).scalar_one()
        )


async def _fetch_thread(sessionmaker, run_id: UUID) -> str:
    async with sessionmaker() as session:
        return (
            await session.execute(
                text("SELECT thread_id FROM workflow_runs WHERE run_id = :rid").bindparams(
                    rid=run_id
                )
            )
        ).scalar_one()


# ---------------------------------------------------------------- task linkage


async def test_create_run_persists_task_id_and_graph_identity(env) -> None:
    runner = Stage4WorkflowRunner(
        env["sessionmaker"],
        object(),
        object(),  # type: ignore[arg-type]
    )
    run = await runner.create_stage4_run(_request(env["task_id"]))

    assert run.task_id == env["task_id"]
    assert run.graph_name == STAGE4_GRAPH_NAME
    assert run.graph_version == STAGE4_GRAPH_VERSION
    # DB 行：task_id 真实落库（NOT NULL + FK），thread_id 与 run 绑定。
    assert await _fetch_thread(env["sessionmaker"], run.run_id) == str(run.run_id)


async def test_create_run_missing_research_task_rejected(env) -> None:
    runner = Stage4WorkflowRunner(
        env["sessionmaker"],
        object(),
        object(),  # type: ignore[arg-type]
    )
    missing = uuid4()
    with pytest.raises(Stage4ResearchTaskNotFound) as excinfo:
        await runner.create_stage4_run(_request(missing))
    assert excinfo.value.code == "stage4_research_task_not_found"
    # 拒绝创建：任务缺失时不为任何 task 产生 run 行。
    assert await _run_count(env["sessionmaker"], missing) == 0


async def test_same_task_concurrent_create_only_one_succeeds(env) -> None:
    """真实 PostgreSQL 并发：同一 task 两个并发 create → 仅一个成功。

    两个请求都先通过 `get_active_for_task` 预检查，靠 partial unique index
    `uq_workflow_runs_one_active_per_task` 兜底：第二个 INSERT 命中唯一冲突
    → IntegrityError → `ActiveWorkflowRunExists`。
    """
    runner = Stage4WorkflowRunner(
        env["sessionmaker"],
        object(),
        object(),  # type: ignore[arg-type]
    )
    request = _request(env["task_id"])

    results = await asyncio.gather(
        runner.create_stage4_run(request),
        runner.create_stage4_run(request),
        return_exceptions=True,
    )

    ok = [r for r in results if not isinstance(r, Exception)]
    rejected = [r for r in results if isinstance(r, Exception)]
    assert len(ok) == 1
    assert len(rejected) == 1
    assert isinstance(rejected[0], ActiveWorkflowRunExists)
    assert rejected[0].code == "active_workflow_run_exists"
    # 恰好一个 run 行（active-run 不变式不被破坏）。
    assert await _run_count(env["sessionmaker"], env["task_id"]) == 1


# ---------------------------------------------------------------- retry vs recovery


async def test_stage1_retry_creates_new_run_and_thread(env, connection_uri) -> None:
    """Stage 1 用户 retry：新 run / 新 thread（recovery ≠ retry）。"""
    checkpoint_manager = LangGraphCheckpointManager(connection_uri)
    runner = WorkflowRunner(env["sessionmaker"], checkpoint_manager)

    run1 = await runner.create_simulation_run(env["task_id"])
    thread1 = await _fetch_thread(env["sessionmaker"], run1.run_id)

    # 第一个 run 到 terminal（completed），解锁 active-run 不变式。
    now = datetime.now(UTC)
    async with env["sessionmaker"]() as session:
        await WorkflowRunRepository(session).mark_completed(run1.run_id, now)
        await session.commit()

    run2 = await runner.create_simulation_run(env["task_id"])
    thread2 = await _fetch_thread(env["sessionmaker"], run2.run_id)

    # retry → 全新 run，全新 thread（thread_id = str(run_id)）。
    assert run2.run_id != run1.run_id
    assert thread2 != thread1
    assert thread2 == str(run2.run_id)
    assert await _run_count(env["sessionmaker"], env["task_id"]) == 2


async def test_stage4_recovery_reuses_same_run_and_thread(env) -> None:
    """Stage 4 internal recovery：FAILED → RUNNING 原地转，同 run / 同 thread。

    `claim_failed_for_recovery` 只把失败 run 认领为 RUNNING 并复用原
    thread_id（= str(run_id)），**不**产生任何新 run 行。
    """
    runner = Stage4WorkflowRunner(
        env["sessionmaker"],
        object(),
        object(),  # type: ignore[arg-type]
    )
    run = await runner.create_stage4_run(_request(env["task_id"]))

    now = datetime.now(UTC)
    async with env["sessionmaker"]() as session:
        repo = WorkflowRunRepository(session)
        claimed = await repo.claim_pending(run.run_id, now)
        assert claimed is not None
        await repo.mark_failed(run.run_id, now, "workflow_execution_failed", "Boom")
        await session.commit()

    async with env["sessionmaker"]() as session:
        recovered = await WorkflowRunRepository(session).claim_failed_for_recovery(run.run_id, now)
        await session.commit()

    # 同 run / 同 task / 同 thread；status FAILED → RUNNING。
    assert recovered is not None
    assert recovered.run_id == run.run_id
    assert recovered.task_id == env["task_id"]
    assert recovered.thread_id == str(run.run_id)
    assert recovered.status == "running"
    # 不产生新 run 行。
    assert await _run_count(env["sessionmaker"], env["task_id"]) == 1
