"""Stage 4 workflow runner (spec N-O): create, execute and resume analysis runs.

镜像 Stage 1 WorkflowRunner 的 short-transaction pattern：
- 短 DB transaction 创建 run / claim pending / 记事件 / finalize；
- graph 执行期间**不持有** DB session（各 Service 内部自管短 session）；
- 复用 LangGraphCheckpointManager（AsyncPostgresSaver）实现 durable execution：
  run 失败后，新 runner + 同 run_id/thread_id 恢复 → 从最后 checkpoint 继续，
  失败节点重跑；Service 幂等（fingerprint / replay）→ 无重复业务对象。

事件（spec P）：只记录 node name / status / item_id / analysis_type / counts /
business IDs；不记录 Evidence text / prompt / raw response / reasoning_content。
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    ActiveWorkflowRunExists,
    WorkflowRunAlreadyFinished,
    WorkflowRunAlreadyStarted,
    WorkflowRunNotFound,
)
from app.db.models.research_orchestration import ResearchOrchestrationChildModel
from app.db.models.workflow_event import WorkflowEventModel
from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import (
    TERMINAL_WORKFLOW_RUN_STATUSES,
    TaskStage,
    WorkflowEventType,
    WorkflowRunStatus,
)
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.workflow import WorkflowRunResponse
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage4.dependencies import Stage4AnalysisDependencies
from app.stage4.errors import Stage4ResearchTaskNotFound
from app.stage4.graph import (
    STAGE4_GRAPH_NAME,
    STAGE4_GRAPH_VERSION,
    build_stage4_analysis_graph,
)
from app.workflows.checkpoint import LangGraphCheckpointManager

_TERMINAL_VALUES = {status.value for status in TERMINAL_WORKFLOW_RUN_STATUSES}
_ALLOWED_NODES = {
    "validate_analysis_plan",
    "run_analysis_item",
    "collect_claim_ids",
    "synthesize_claims",
}
_NODE_STAGE = {
    "validate_analysis_plan": TaskStage.ANALYZING,
    "run_analysis_item": TaskStage.ANALYZING,
    "collect_claim_ids": TaskStage.ANALYZING,
    "synthesize_claims": TaskStage.SYNTHESIZING,
}
_ERROR_CODE = "workflow_execution_failed"
_MAX_ERROR_MESSAGE_LENGTH = 200


def _sanitize_error(exc: Exception) -> str:
    """Return a short, stable, sanitised error description (exception type only)."""
    return type(exc).__name__[:_MAX_ERROR_MESSAGE_LENGTH]


class Stage4WorkflowRunner:
    """Runs a Stage 4 analysis graph without holding any DB transaction while it runs."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
        dependencies: Stage4AnalysisDependencies,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._checkpoint_manager = checkpoint_manager
        self._dependencies = dependencies

    # ------------------------------------------------------------------ create

    async def create_stage4_run(
        self,
        request: Stage4WorkflowRequest,
        *,
        child_bind: Callable[[UUID], ResearchOrchestrationChildModel] | None = None,
    ) -> WorkflowRunResponse:
        """创建 Stage 4 工作流 run（必须绑定一个真实 ResearchTask）。

        - 真实 PG 校验 task 存在 → 缺失 `Stage4ResearchTaskNotFound`（不猜任务、
          不自动创建 fake ResearchTask）；
        - active-run 不变式：同一 task 同时只能存在一个 active WorkflowRun——
          先查 `get_active_for_task`，再靠 partial unique index
          `uq_workflow_runs_one_active_per_task` 兜底并发（IntegrityError →
          `ActiveWorkflowRunExists`）；
        - `child_bind`（orchestration ownership，spec K）：可选 factory
          `run_id → ResearchOrchestrationChildModel`。**WorkflowRun 行 + child
          link 在同一事务提交**（7A.2B.1 选择 same-transaction，而非
          runner self-commit 后补 link）——run create 与 child link insert 之间
          无 crash 孤儿窗口；任一唯一约束冲突（active index /
          `UNIQUE(workflow_run_id)` / `UNIQUE(orchestration_id, stage, attempt_no)`）
          → 整个事务回滚，`ActiveWorkflowRunExists` 交调用方
          （`ensure_stage4_child` 重查 exact child → 返回 winner）。
        """
        run_id = uuid.uuid4()
        async with self._sessionmaker() as session:
            task_repo = ResearchTaskRepository(session)
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            task = await task_repo.get_by_id(request.task_id)
            if task is None:
                raise Stage4ResearchTaskNotFound()
            active = await run_repo.get_active_for_task(request.task_id)
            if active is not None:
                raise ActiveWorkflowRunExists()
            run = WorkflowRunModel(
                run_id=run_id,
                task_id=request.task_id,
                thread_id=str(run_id),
                graph_name=STAGE4_GRAPH_NAME,
                graph_version=STAGE4_GRAPH_VERSION,
                status=WorkflowRunStatus.PENDING.value,
            )
            try:
                await run_repo.create(run)
                if child_bind is not None:
                    session.add(child_bind(run_id))
                await event_repo.create(
                    WorkflowEventModel(
                        run_id=run_id,
                        event_type=WorkflowEventType.RUN_CREATED.value,
                        stage=TaskStage.ANALYZING.value,
                        progress=0,
                        message="Stage 4 分析工作流已创建",
                        payload={
                            "graph_name": STAGE4_GRAPH_NAME,
                            "graph_version": STAGE4_GRAPH_VERSION,
                        },
                    )
                )
                await session.commit()
            except IntegrityError:
                # 并发创建同一 task 的两个 active run（unique partial index）或
                # orchestration child link 归属冲突（UNIQUE(workflow_run_id) /
                # UNIQUE(orchestration_id, stage, attempt_no)）：整个事务回滚，
                # 由调用方（ensure_stage4_child）重查 exact child → 返回 winner。
                await session.rollback()
                raise ActiveWorkflowRunExists() from None
            return WorkflowRunResponse.model_validate(run)

    # ------------------------------------------------------------------ execute

    async def execute_stage4(self, run_id: UUID, request: Stage4WorkflowRequest) -> dict:
        """首次执行：claim pending → 构造 initial state → 跑 graph → finalize。"""
        started_at = datetime.now(UTC)
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
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
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_STARTED.value,
                    stage=TaskStage.ANALYZING.value,
                    progress=0,
                    message="Stage 4 分析工作流开始执行",
                    payload={},
                )
            )
            await session.commit()

        initial_state = self._build_initial_state(request)
        return await self._run_graph(run_id, thread_id, initial_state)

    # ------------------------------------------------------------------ resume

    async def resume_stage4(self, run_id: UUID) -> dict:
        """失败后内部 durable recovery：FAILED → RUNNING，同 run/thread 从最后 checkpoint 继续。

        **recovery ≠ retry**：仅用于 Stage 4 内部恢复，复用同 run / thread；
        Stage 1 用户 retry 语义不变（create_simulation_run → 新 run / 新 thread）。
        """
        started_at = datetime.now(UTC)
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            claimed = await run_repo.claim_failed_for_recovery(run_id, started_at)
            if claimed is None:
                run = await run_repo.get_by_id(run_id)
                if run is None:
                    raise WorkflowRunNotFound()
                if run.status in _TERMINAL_VALUES:
                    raise WorkflowRunAlreadyFinished()
                raise WorkflowRunAlreadyStarted()
            thread_id = claimed.thread_id
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_RESUMED.value,
                    stage=TaskStage.ANALYZING.value,
                    progress=0,
                    message="Stage 4 分析工作流恢复执行",
                    payload={},
                )
            )
            await session.commit()

        return await self._run_graph(run_id, thread_id, None)

    # ------------------------------------------------------------------ checkpoint

    async def read_checkpoint_state(self, run_id: UUID) -> dict:
        """只读读取已持久化 checkpoint state（恢复协调器续接 Stage5 用）。

        不 claim、不改 run 状态；run 不存在 → `WorkflowRunNotFound`。返回
        `dict(state.values)`，字段与 `_build_initial_state` 一致（company_id /
        research_question / analysis_as_of / synthesis_result_id …）。
        """
        async with self._sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_stage4_analysis_graph(self._dependencies, checkpointer)
        state = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
        return dict(state.values) if state is not None else {}

    # ------------------------------------------------------------------ internal

    async def _run_graph(
        self,
        run_id: UUID,
        thread_id: str,
        initial_state: dict | None,
    ) -> dict:
        """共享执行路径：graph 运行期间不持有 DB session。"""
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_stage4_analysis_graph(self._dependencies, checkpointer)
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

    @staticmethod
    def _build_initial_state(request: Stage4WorkflowRequest) -> dict:
        """把 request 投影成 checkpoint-safe initial state（UUID 统一 string）。"""
        return {
            "company_id": str(request.company_id),
            "research_question": request.research_question,
            "analysis_as_of": request.analysis_as_of.isoformat(),
            "analysis_work_items": [
                item.model_dump(mode="json") for item in request.analysis_work_items
            ],
            "analysis_results": [],
            "claim_ids": [],
            "synthesis_id": None,
            "synthesis_result_id": None,
        }

    async def _finalize(self, run_id: UUID, final_state) -> dict:
        result = dict(final_state.values) if final_state is not None else {}
        await self._mark_completed(run_id)
        return result

    async def _persist_node_event(self, run_id: UUID, update: dict) -> None:
        for node_name, node_update in update.items():
            if node_name not in _ALLOWED_NODES:
                continue
            payload = self._node_payload(node_name, node_update)
            async with self._sessionmaker() as session:
                event_repo = WorkflowEventRepository(session)
                await event_repo.create(
                    WorkflowEventModel(
                        run_id=run_id,
                        event_type=WorkflowEventType.NODE_COMPLETED.value,
                        node_name=node_name,
                        stage=_NODE_STAGE[node_name].value,
                        progress=None,
                        message=f"节点完成: {node_name}",
                        payload=payload,
                    )
                )
                await session.commit()

    @staticmethod
    def _node_payload(node_name: str, node_update: dict) -> dict:
        """事件 payload：只含 node 名 / 状态 / item_id / analysis_type / counts /
        business IDs，不含 Evidence text / prompt / raw response / reasoning。"""
        if node_name == "validate_analysis_plan":
            return {"status": "ok"}
        if node_name == "run_analysis_item":
            results = node_update.get("analysis_results") or []
            if not results:
                return {}
            item = results[0]
            return {
                "item_id": item["item_id"],
                "analysis_type": item["analysis_type"],
                "claim_count": len(item["claim_ids"]),
            }
        if node_name == "collect_claim_ids":
            return {"claim_count": len(node_update.get("claim_ids") or [])}
        if node_name == "synthesize_claims":
            return {
                "synthesis_id": node_update.get("synthesis_id"),
                "synthesis_result_id": node_update.get("synthesis_result_id"),
                "claim_count": node_update.get("claim_count"),
            }
        return {}

    async def _mark_completed(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            await run_repo.mark_completed(run_id, datetime.now(UTC))
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_COMPLETED.value,
                    stage=TaskStage.SYNTHESIZING.value,
                    progress=100,
                    message="Stage 4 分析工作流完成",
                    payload={"synthesis_complete": True},
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
                    stage=TaskStage.ANALYZING.value,
                    message="Stage 4 分析工作流失败",
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
