"""Workflow runner: creates, claims, executes and resumes simulation runs."""

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

from langgraph.types import Command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    ActiveWorkflowRunExists,
    TaskNotFound,
    WorkflowRunAlreadyFinished,
    WorkflowRunAlreadyStarted,
    WorkflowRunNotFound,
)
from app.db.models.human_action import HumanActionModel
from app.db.models.workflow_event import WorkflowEventModel
from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import (
    TERMINAL_WORKFLOW_RUN_STATUSES,
    HumanActionType,
    TaskStage,
    WorkflowEventType,
    WorkflowRunStatus,
)
from app.repositories.human_action_repository import HumanActionRepository
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowRunResponse
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.graph import GRAPH_NAME, GRAPH_VERSION, build_research_workflow

_TERMINAL_VALUES = {status.value for status in TERMINAL_WORKFLOW_RUN_STATUSES}
_ALLOWED_NODES = {
    "load_task_context",
    "build_research_plan",
    "request_plan_approval",
    "finish_simulation",
}
_ERROR_CODE = "workflow_execution_failed"
_MAX_ERROR_MESSAGE_LENGTH = 200


def _sanitize_error(exc: Exception) -> str:
    """Return a short, stable, sanitised error description (exception type only)."""
    return type(exc).__name__[:_MAX_ERROR_MESSAGE_LENGTH]


def _has_interrupt(state) -> bool:
    return bool(state.tasks) and any(task.interrupts for task in state.tasks)


@dataclass
class ResumePreparation:
    run_id: UUID
    thread_id: str
    action_type: HumanActionType


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
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            task_repo = ResearchTaskRepository(session)
            event_repo = WorkflowEventRepository(session)
            claimed = await run_repo.claim_pending(run_id, started_at)
            if claimed is None:
                run = await run_repo.get_by_id(run_id)
                if run is None:
                    raise WorkflowRunNotFound()
                if run.status in _TERMINAL_VALUES:
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
                "require_plan_approval": task.require_plan_approval,
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
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_failed(run_id, exc)
            raise
        return await self._finalize(run_id, final_state)

    async def prepare_resume(
        self,
        run_id: UUID,
        action_type: HumanActionType,
    ) -> ResumePreparation:
        """Atomically accept a resume in one short transaction; returns preparation on success."""
        if action_type != HumanActionType.APPROVE_PLAN:
            raise ValueError("unsupported human action")
        started_at = datetime.now(UTC)
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            human_repo = HumanActionRepository(session)
            claimed = await run_repo.claim_waiting_human(run_id, started_at)
            if claimed is None:
                run = await run_repo.get_by_id(run_id)
                if run is None:
                    raise WorkflowRunNotFound()
                if run.status in _TERMINAL_VALUES:
                    raise WorkflowRunAlreadyFinished()
                raise WorkflowRunAlreadyStarted()
            thread_id = claimed.thread_id
            try:
                await human_repo.create(
                    HumanActionModel(
                        run_id=run_id,
                        interrupt_key="plan_approval",
                        action_type=action_type.value,
                        payload={},
                    )
                )
            except IntegrityError:
                # UNIQUE(run_id, interrupt_key) 是重复提交的最终防线
                await session.rollback()
                raise WorkflowRunAlreadyFinished() from None
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_RESUMED.value,
                    message="工作流运行已恢复",
                    payload={},
                )
            )
            await session.commit()
        return ResumePreparation(run_id=run_id, thread_id=thread_id, action_type=action_type)

    async def continue_resume(self, preparation: ResumePreparation) -> dict:
        """Resume the graph using the already-accepted preparation; no DB claim here."""
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_research_workflow(checkpointer)
        config = {"configurable": {"thread_id": preparation.thread_id}}

        try:
            async for update in graph.astream(
                Command(resume={"action_type": preparation.action_type.value}),
                config,
                stream_mode="updates",
            ):
                await self._persist_node_event(preparation.run_id, update)
            final_state = await graph.aget_state(config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_failed(preparation.run_id, exc)
            raise
        return await self._finalize(preparation.run_id, final_state)

    async def _finalize(self, run_id: UUID, final_state) -> dict:
        result = dict(final_state.values) if final_state is not None else {}
        if final_state is not None and _has_interrupt(final_state):
            await self._mark_waiting_human(run_id)
            return result
        await self._mark_completed(run_id, result.get("current_stage"))
        return result

    async def _mark_waiting_human(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            updated = await run_repo.mark_waiting_human(run_id, "plan_approval")
            if updated is None:
                return
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_WAITING_HUMAN.value,
                    message="等待人工确认研究计划",
                    payload={"pending_action": "plan_approval"},
                )
            )
            await session.commit()

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
        updated = await self.get_run(run.run_id)
        return updated, result
