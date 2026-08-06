"""Workflow runner: creates, claims and executes simulation runs with event persistence."""

import asyncio
import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    ActiveWorkflowRunExists,
    TaskNotFound,
    WorkflowRunAlreadyFinished,
    WorkflowRunAlreadyStarted,
    WorkflowRunNotFound,
)
from app.db.models.workflow_event import WorkflowEventModel
from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import TaskStage, WorkflowEventType, WorkflowRunStatus
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowRunResponse
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.graph import GRAPH_NAME, GRAPH_VERSION, build_research_workflow

_FINISHED_STATUSES = {
    WorkflowRunStatus.COMPLETED.value,
    WorkflowRunStatus.FAILED.value,
    WorkflowRunStatus.CANCELLED.value,
}
_ALLOWED_NODES = {"load_task_context", "build_research_plan", "finish_simulation"}
_ERROR_CODE = "workflow_execution_failed"
_MAX_ERROR_MESSAGE_LENGTH = 200


def _sanitize_error(exc: Exception) -> str:
    """Return a short, stable, sanitised error description (exception type only)."""
    return type(exc).__name__[:_MAX_ERROR_MESSAGE_LENGTH]


class WorkflowRunner:
    """Runs a LangGraph simulation without holding any DB transaction while the graph runs."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._checkpoint_manager = checkpoint_manager

    async def create_simulation_run(self, task_id: UUID) -> WorkflowRunResponse:
        run_id = uuid.uuid4()
        async with self._sessionmaker() as session:
            task_repo = ResearchTaskRepository(session)
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            task = await task_repo.get_by_id(task_id)
            if task is None:
                raise TaskNotFound()
            active = await run_repo.get_active_for_task(task_id)
            if active is not None:
                raise ActiveWorkflowRunExists()
            run = WorkflowRunModel(
                run_id=run_id,
                task_id=task_id,
                thread_id=str(run_id),
                graph_name=GRAPH_NAME,
                graph_version=GRAPH_VERSION,
                status=WorkflowRunStatus.PENDING.value,
            )
            await run_repo.create(run)
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_CREATED.value,
                    stage=TaskStage.CREATED.value,
                    progress=0,
                    message="工作流运行已创建",
                    payload={"graph_name": GRAPH_NAME, "graph_version": GRAPH_VERSION},
                )
            )
            await session.commit()
            return WorkflowRunResponse.model_validate(run)

    async def execute_simulation(self, run_id: UUID) -> dict:
        started_at = datetime.now(UTC)
        # 第一段短事务：原子领取 + 构建初始状态 + run_started 事件
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            task_repo = ResearchTaskRepository(session)
            event_repo = WorkflowEventRepository(session)
            claimed = await run_repo.claim_pending(run_id, started_at)
            if claimed is None:
                run = await run_repo.get_by_id(run_id)
                if run is None:
                    raise WorkflowRunNotFound()
                if run.status in _FINISHED_STATUSES:
                    raise WorkflowRunAlreadyFinished()
                raise WorkflowRunAlreadyStarted()
            thread_id = claimed.thread_id
            task = await task_repo.get_by_id(claimed.task_id)
            if task is None:
                raise TaskNotFound()
            initial_state = {
                "task_id": str(task.task_id),
                "run_id": str(run_id),
                "company_query": task.company_query,
                "modules": task.modules,
                "questions": task.questions,
                "current_stage": task.current_stage,
                "progress": task.progress,
            }
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_STARTED.value,
                    stage=TaskStage.CREATED.value,
                    progress=0,
                    message="工作流运行开始执行",
                    payload={},
                )
            )
            await session.commit()
            # 短事务结束；graph 执行期间不持有 session

        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_research_workflow(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        try:
            async for update in graph.astream(initial_state, config, stream_mode="updates"):
                await self._persist_node_event(run_id, update)
            final_state = await graph.aget_state(config)
            result = dict(final_state.values) if final_state is not None else {}
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_failed(run_id, exc)
            raise
        else:
            await self._mark_completed(run_id, result.get("current_stage"))
            return result

    async def _persist_node_event(self, run_id: UUID, update: dict) -> None:
        for node_name, node_update in update.items():
            if node_name not in _ALLOWED_NODES:
                continue
            payload: dict = {}
            if "completed_nodes" in node_update:
                payload["completed_nodes"] = node_update["completed_nodes"]
            if "simulation_complete" in node_update:
                payload["simulation_complete"] = node_update["simulation_complete"]
            async with self._sessionmaker() as session:
                event_repo = WorkflowEventRepository(session)
                await event_repo.create(
                    WorkflowEventModel(
                        run_id=run_id,
                        event_type=WorkflowEventType.NODE_COMPLETED.value,
                        node_name=node_name,
                        stage=node_update.get("current_stage"),
                        progress=node_update.get("progress"),
                        message=f"节点完成: {node_name}",
                        payload=payload,
                    )
                )
                await session.commit()

    async def _mark_completed(self, run_id: UUID, stage: str | None) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            await run_repo.mark_completed(run_id, datetime.now(UTC))
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_COMPLETED.value,
                    stage=stage,
                    progress=100,
                    message="工作流运行完成",
                    payload={"simulation_complete": True},
                )
            )
            await session.commit()

    async def _mark_failed(self, run_id: UUID, exc: Exception) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            await run_repo.mark_failed(
                run_id,
                datetime.now(UTC),
                _ERROR_CODE,
                _sanitize_error(exc),
            )
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_FAILED.value,
                    message="工作流运行失败",
                    payload={},
                )
            )
            await session.commit()

    async def get_run(self, run_id: UUID) -> WorkflowRunResponse:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            run = await run_repo.get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        return WorkflowRunResponse.model_validate(run)

    async def create_and_execute(self, task_id: UUID) -> tuple[WorkflowRunResponse, dict]:
        run = await self.create_simulation_run(task_id)
        result = await self.execute_simulation(run.run_id)
        # 重新读取执行后的最新状态
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            updated = await run_repo.get_by_id(run.run_id)
        if updated is None:
            raise WorkflowRunNotFound()
        return WorkflowRunResponse.model_validate(updated), result
