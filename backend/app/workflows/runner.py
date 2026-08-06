"""Workflow runner: creates and executes simulation runs."""

import uuid
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    ActiveWorkflowRunExists,
    TaskNotFound,
    WorkflowRunAlreadyFinished,
    WorkflowRunNotFound,
)
from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import WorkflowRunStatus
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowRunResponse
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.graph import GRAPH_NAME, GRAPH_VERSION, build_research_workflow

_FINISHED_STATUSES = {
    WorkflowRunStatus.COMPLETED.value,
    WorkflowRunStatus.FAILED.value,
    WorkflowRunStatus.CANCELLED.value,
}
_ERROR_CODE = "graph_execution_failed"
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
            await session.commit()
            return WorkflowRunResponse.model_validate(run)

    async def execute_simulation(self, run_id: UUID) -> dict:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            task_repo = ResearchTaskRepository(session)
            run = await run_repo.get_by_id(run_id)
            if run is None:
                raise WorkflowRunNotFound()
            if run.status in _FINISHED_STATUSES:
                raise WorkflowRunAlreadyFinished()
            task = await task_repo.get_by_id(run.task_id)
            if task is None:
                raise TaskNotFound()
            thread_id = run.thread_id
            initial_state = {
                "task_id": str(task.task_id),
                "run_id": str(run.run_id),
                "company_query": task.company_query,
                "modules": task.modules,
                "questions": task.questions,
                "current_stage": task.current_stage,
                "progress": task.progress,
            }
            # 短事务在此结束；graph 执行期间不持有 session

        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_research_workflow(checkpointer)
        config = {"configurable": {"thread_id": thread_id}}

        try:
            result = await graph.ainvoke(initial_state, config)
        except Exception as exc:
            async with self._sessionmaker() as session:
                run_repo = WorkflowRunRepository(session)
                await run_repo.mark_failed(
                    run_id,
                    datetime.now(UTC),
                    _ERROR_CODE,
                    _sanitize_error(exc),
                )
                await session.commit()
            raise
        else:
            async with self._sessionmaker() as session:
                run_repo = WorkflowRunRepository(session)
                await run_repo.mark_completed(run_id, datetime.now(UTC))
                await session.commit()
            return result

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
