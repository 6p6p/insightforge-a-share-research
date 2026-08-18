"""Stage 5 report control workflow runner (spec D/O/Q): create, execute, human resume.

镜像 Stage 4 runner 的 short-transaction pattern + Stage 1 的人类中断 semantics：
- 短 DB transaction 创建 run / claim pending / 记事件 / finalize；
- graph 执行期间**不持有** DB session（各 Service 内部自管短 session）；
- 复用 LangGraphCheckpointManager（AsyncPostgresSaver）实现 durable execution：
  run 失败后，新 runner + 同 run_id/thread_id 恢复 → 从最后 checkpoint 继续，
  失败节点重跑；Service 幂等（fingerprint / replay）→ 无重复业务对象；
- 人审（spec Q）：graph 真实 `interrupt()` → run 置 WAITING_HUMAN；
  `resume_stage5_human` 先经 `ReviewActionService.resolve_human_request` 持久化
  immutable HumanReviewDecision，再 `Command(resume=...)` 恢复 graph；
- terminal 映射：finalize / research_required → COMPLETED；revision_limit_exceeded
  → FAILED（spec O）；cancelled → CANCELLED（spec Q 人工取消）。

事件（spec P）：只记录 node name / status / business IDs / counts / decision；
不记录 Evidence text / prompt / raw response / reasoning_content / comment 全文。
"""

import asyncio
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from langgraph.types import Command
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.errors import (
    ActiveWorkflowRunExists,
    WorkflowRunAlreadyFinished,
    WorkflowRunAlreadyStarted,
    WorkflowRunNotFound,
)
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
from app.services.workflow_recovery_service import WORKER_RESTARTED_ERROR_CODE
from app.stage5.contracts import (
    STAGE5_GRAPH_NAME,
    STAGE5_GRAPH_VERSION,
    STAGE5_TERMINAL_CANCELLED,
    STAGE5_TERMINAL_RESEARCH_REQUIRED,
    STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED,
    Stage5WorkflowRequest,
)
from app.stage5.dependencies import Stage5WorkflowDependencies
from app.stage5.errors import (
    Stage5InvalidState,
    Stage5NoPendingHumanReview,
    Stage5ResearchTaskNotFound,
)
from app.stage5.graph import build_stage5_report_graph
from app.workflows.checkpoint import LangGraphCheckpointManager

_TERMINAL_VALUES = {status.value for status in TERMINAL_WORKFLOW_RUN_STATUSES}
_ALLOWED_NODES = {
    "build_report_draft",
    "assemble_report",
    "check_report",
    "audit_report",
    "route_action",
    "rewrite_sections",
    "wait_human",
    "finalize_on_approve",
    "create_research_backflow_request",
}
_NODE_STAGE = {
    "build_report_draft": TaskStage.WRITING,
    "assemble_report": TaskStage.WRITING,
    "check_report": TaskStage.CHECKING,
    "audit_report": TaskStage.AUDITING,
    "route_action": TaskStage.AUDITING,
    "rewrite_sections": TaskStage.WRITING,
    "wait_human": TaskStage.AUDITING,
    "finalize_on_approve": TaskStage.AUDITING,
    "create_research_backflow_request": TaskStage.AUDITING,
}
_ERROR_CODE = "workflow_execution_failed"
_MAX_ERROR_MESSAGE_LENGTH = 200


def _sanitize_error(exc: Exception) -> str:
    """Return a short, stable, sanitised error description (exception type only)."""
    return type(exc).__name__[:_MAX_ERROR_MESSAGE_LENGTH]


def _constraint_name(exc: IntegrityError) -> str | None:
    """从 PostgreSQL Diagnostics 取违反的约束名（无 diag / 非约束错误 → None）。"""
    diag = getattr(getattr(exc, "orig", None), "diag", None)
    return getattr(diag, "constraint_name", None) if diag is not None else None


def _has_interrupt(state) -> bool:
    return bool(state.tasks) and any(task.interrupts for task in state.tasks)


class Stage5WorkflowRunner:
    """Runs a Stage 5 report control graph without holding any DB transaction while it runs."""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
        dependencies: Stage5WorkflowDependencies,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._checkpoint_manager = checkpoint_manager
        self._dependencies = dependencies
        # research backflow 只能由 runner 注入 Stage5 checkpoint + deps（service
        # 内部 `_recover_final_state` 需要读 checkpoint；未绑定 → 明确拒绝）。
        dependencies.research_backflow_service.bind_stage5(checkpoint_manager, dependencies)

    # ------------------------------------------------------------------ create

    async def create_stage5_run(
        self,
        request: Stage5WorkflowRequest,
        *,
        on_run_created: Callable[[AsyncSession, UUID], None] | None = None,
    ) -> WorkflowRunResponse:
        """创建 Stage 5 工作流 run（必须绑定一个真实 ResearchTask）。

        - 真实 PG 校验 task 存在 → 缺失 `Stage5ResearchTaskNotFound`（不猜任务、
          不自动创建 fake ResearchTask）；
        - active-run 不变式：同一 task 同时只能存在一个 active WorkflowRun——
          先查 `get_active_for_task`，再靠 partial unique index
          `uq_workflow_runs_one_active_per_task` 兜底并发（IntegrityError →
          `ActiveWorkflowRunExists`）；
        - `on_run_created`（orchestration ownership，spec K/F）：可选 transaction
          hook `(AsyncSession, run_id) → None`。**WorkflowRun 行 + hook 副作用
          在同一事务提交**（run create 与 child link insert 之间无 crash 孤儿
          窗口）；runner **不 import orchestration 层**；
        - **IntegrityError 分类（spec E）**：只有 `uq_workflow_runs_one_active_per_task`
          → `ActiveWorkflowRunExists`；child ownership 约束与其它唯一冲突原样抛，
          由 orchestration 层分类。
        """
        run_id = uuid.uuid4()
        async with self._sessionmaker() as session:
            task_repo = ResearchTaskRepository(session)
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            task = await task_repo.get_by_id(request.task_id)
            if task is None:
                raise Stage5ResearchTaskNotFound()
            active = await run_repo.get_active_for_task(request.task_id)
            if active is not None:
                raise ActiveWorkflowRunExists()
            run = WorkflowRunModel(
                run_id=run_id,
                task_id=request.task_id,
                thread_id=str(run_id),
                graph_name=STAGE5_GRAPH_NAME,
                graph_version=STAGE5_GRAPH_VERSION,
                status=WorkflowRunStatus.PENDING.value,
            )
            try:
                await run_repo.create(run)
                if on_run_created is not None:
                    on_run_created(session, run_id)
            except IntegrityError as exc:
                # 并发创建同一 task 的两个 active run：unique partial index 兜底 →
                # 只转 ActiveWorkflowRunExists；child link 归属冲突原样抛。
                await session.rollback()
                if _constraint_name(exc) == "uq_workflow_runs_one_active_per_task":
                    raise ActiveWorkflowRunExists() from None
                raise
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_CREATED.value,
                    stage=TaskStage.WRITING.value,
                    progress=0,
                    message="Stage 5 报告控制流已创建",
                    payload={
                        "graph_name": STAGE5_GRAPH_NAME,
                        "graph_version": STAGE5_GRAPH_VERSION,
                    },
                )
            )
            await session.commit()
            return WorkflowRunResponse.model_validate(run)

    # ------------------------------------------------------------------ execute

    async def execute_stage5(self, run_id: UUID, request: Stage5WorkflowRequest) -> dict:
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
                    stage=TaskStage.WRITING.value,
                    progress=0,
                    message="Stage 5 报告控制流开始执行",
                    payload={},
                )
            )
            await session.commit()

        initial_state = self._build_initial_state(request, run_id)
        return await self._run_graph(run_id, thread_id, initial_state=initial_state)

    # ------------------------------------------------------------------ human resume

    async def resume_stage5_human(
        self,
        run_id: UUID,
        decision: str,
        comment: str | None = None,
    ) -> dict:
        """WAITING_HUMAN run 人工裁决 → 恢复 graph（spec Q/R/S）。

        1. 从 checkpoint state 取 human_request_id（read-only）；
        2. `resolve_human_request` 持久化 immutable HumanReviewDecision（同 decision/
           comment → replay；不同 → AlreadyResolved，**不覆盖历史**）；
        3. 原子 claim_waiting_human（重复提交 → AlreadyFinished/AlreadyStarted）；
        4. `Command(resume={human_decision_id, decision, comment})` 恢复 graph。
        """
        # 1-2. resolve 先于 claim：resolve 失败（非法 decision 等）不占用 run。
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_stage5_report_graph(self._dependencies, checkpointer)
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            run = await run_repo.get_by_id(run_id)
            if run is None:
                raise WorkflowRunNotFound()
            thread_id = run.thread_id
        config = {"configurable": {"thread_id": thread_id}}
        state = await graph.aget_state(config)
        human_request_id = (state.values or {}).get("human_request_id")
        if not human_request_id:
            raise Stage5NoPendingHumanReview()
        dec = await self._dependencies.review_action_service.resolve_human_request(
            UUID(human_request_id), decision, comment
        )

        # 3. 原子 claim。
        started_at = datetime.now(UTC)
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            claimed = await run_repo.claim_waiting_human(run_id, started_at)
            if claimed is None:
                run = await run_repo.get_by_id(run_id)
                if run is None:
                    raise WorkflowRunNotFound()
                if run.status in _TERMINAL_VALUES:
                    raise WorkflowRunAlreadyFinished()
                raise WorkflowRunAlreadyStarted()
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_RESUMED.value,
                    stage=TaskStage.AUDITING.value,
                    progress=None,
                    message="Stage 5 人工裁决已提交，恢复执行",
                    payload={
                        "human_decision_id": str(dec.human_decision_id),
                        "decision": decision,
                    },
                )
            )
            await session.commit()

        # 4. 恢复 graph（wait_human 的 interrupt() 返回 resume value 后继续）。
        resume_value = {
            "human_decision_id": str(dec.human_decision_id),
            "decision": decision,
            "comment": dec.comment,
        }
        return await self._run_graph(run_id, thread_id, resume=resume_value)

    async def resume_stage5_for_recovery(
        self,
        run_id: UUID,
        request: Stage5WorkflowRequest | None = None,
    ) -> dict:
        """失败后内部 durable recovery：FAILED(worker_restarted) → RUNNING，同 run/thread 恢复。

        **recovery ≠ retry**：仅用于 Stage 5 worker 重启中断恢复（镜像 Stage 4
        `resume_stage4`）。claim 只匹配 `stage5_report` + `failed` +
        `error_code=worker_restarted`（`claim_failed_for_recovery_gated`）——业务
        失败（LLM / 校验 / 终态错误）不在该路径，WAITING_HUMAN 人工裁决走
        `resume_stage5_human`。graph 从最后 checkpoint 继续（无 initial_state），
        `_finalize` 统一处理 interrupt（→ waiting_human）与 terminal。

        **从未执行的 run**（crash 在 `execute_stage5` 之前，无 checkpoint）→
        复用同 run/thread 重新首启：`request` 由 orchestration 节点从 Stage4
        checkpoint 投影（与 `execute_stage5` 相同的 initial_state），否则
        `Stage5InvalidState`（防御性，不该发生）。
        """
        started_at = datetime.now(UTC)
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            claimed = await run_repo.claim_failed_for_recovery_gated(
                run_id,
                started_at,
                graph_name=STAGE5_GRAPH_NAME,
                error_code=WORKER_RESTARTED_ERROR_CODE,
            )
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
                    stage=TaskStage.WRITING.value,
                    progress=0,
                    message="Stage 5 报告控制流恢复执行（worker 重启恢复）",
                    payload={},
                )
            )
            await session.commit()

        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_stage5_report_graph(self._dependencies, checkpointer)
        state = await graph.aget_state({"configurable": {"thread_id": thread_id}})
        if state is None or not state.values:
            if request is None:
                raise Stage5InvalidState("cannot recover unstarted stage5 run without request")
            initial_state = self._build_initial_state(request, run_id)
            return await self._run_graph(run_id, thread_id, initial_state=initial_state)
        return await self._run_graph(run_id, thread_id)

    # ------------------------------------------------------------------ checkpoint

    @property
    def dependencies(self) -> Stage5WorkflowDependencies:
        """只读访问已装配的 Stage 5 deps（artifact workspace 复用 verify 服务）。"""
        return self._dependencies

    async def read_checkpoint_state(self, run_id: UUID) -> dict:
        """只读读取已持久化 checkpoint state（research artifact workspace 用）。

        镜像 Stage4WorkflowRunner.read_checkpoint_state：不 claim、不改 run
        状态；run 不存在 → `WorkflowRunNotFound`。返回 `dict(state.values)`，
        字段与 `_build_initial_state` 一致（report_id / audit_id /
        review_action_id …）。
        """
        async with self._sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_stage5_report_graph(self._dependencies, checkpointer)
        state = await graph.aget_state({"configurable": {"thread_id": run.thread_id}})
        return dict(state.values) if state is not None else {}

    # ------------------------------------------------------------------ internal

    async def _run_graph(
        self,
        run_id: UUID,
        thread_id: str,
        initial_state: dict | None = None,
        resume: dict | None = None,
    ) -> dict:
        """共享执行路径：graph 运行期间不持有 DB session。"""
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_stage5_report_graph(self._dependencies, checkpointer)
        config = {"configurable": {"thread_id": thread_id}}
        try:
            if resume is not None:
                async for update in graph.astream(
                    Command(resume=resume), config, stream_mode="updates"
                ):
                    await self._persist_node_event(run_id, update)
            else:
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
    def _build_initial_state(request: Stage5WorkflowRequest, run_id: UUID) -> dict:
        """把 request 投影成 checkpoint-safe initial state（UUID 统一 string）。

        `source_stage5_run_id` 由 runner 注入（research_required terminal 时
        create_research_backflow_request 节点用它创建 research 交接请求）。
        """
        return {
            "task_id": str(request.task_id),
            "company_id": str(request.company_id),
            "research_question": request.research_question,
            "analysis_as_of": request.analysis_as_of.isoformat(),
            "synthesis_result_id": str(request.synthesis_result_id),
            "source_stage5_run_id": str(run_id),
            "outline_id": None,
            "sections": [],
            "report_id": None,
            "check_result_id": None,
            "audit_id": None,
            "review_action_id": None,
            "route": None,
            "revision_round": 1,
            "revision_target_section_ids": [],
            "revision_trigger_type": None,
            "revision_trigger_artifact_id": None,
            "revisions": [],
            "human_request_id": None,
            "human_decision_id": None,
            "human_decision": None,
            "human_comment": None,
            "terminal": None,
        }

    async def _finalize(self, run_id: UUID, final_state) -> dict:
        result = dict(final_state.values) if final_state is not None else {}
        if final_state is not None and _has_interrupt(final_state):
            await self._mark_waiting_human(run_id)
            return result
        terminal = result.get("terminal")
        if terminal == STAGE5_TERMINAL_RESEARCH_REQUIRED:
            # 终态不变式（spec Q）：research_required 必带 research_request_id +
            # review_action_id + report_id（+ human_decision_id 由 create node 保证）。
            missing = [
                key
                for key in ("research_request_id", "review_action_id", "report_id")
                if not result.get(key)
            ]
            if missing:
                await self._mark_failed(
                    run_id, Stage5InvalidState(f"research_required terminal missing {missing}")
                )
                raise Stage5InvalidState(
                    "research_required terminal must carry research_request_id, "
                    "review_action_id and report_id"
                )
        if terminal == STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED:
            await self._mark_revision_limit_exceeded(run_id)
        elif terminal == STAGE5_TERMINAL_CANCELLED:
            await self._mark_cancelled(run_id)
        else:
            await self._mark_completed(run_id, terminal)
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
        """事件 payload：只含 node 名 / business IDs / counts / decision 枚举，
        不含 Evidence text / prompt / raw response / comment 全文。"""
        if node_name == "build_report_draft":
            sections = node_update.get("sections") or []
            degraded_count = node_update.get("degraded_section_count", 0)
            total_count = node_update.get("section_count", len(sections))
            return {
                "outline_id": node_update.get("outline_id"),
                "section_count": total_count,
                "degraded_section_count": degraded_count,
            }
        if node_name == "assemble_report":
            return {
                "report_id": node_update.get("report_id"),
                "assembled_section_count": node_update.get("assembled_section_count"),
                "degraded_section_count": node_update.get("degraded_section_count"),
            }
        if node_name == "check_report":
            return {"check_result_id": node_update.get("check_result_id")}
        if node_name == "audit_report":
            return {"audit_id": node_update.get("audit_id")}
        if node_name == "route_action":
            return {
                "route": node_update.get("route"),
                "review_action_id": node_update.get("review_action_id"),
                "terminal": node_update.get("terminal"),
            }
        if node_name == "rewrite_sections":
            revisions = node_update.get("revisions") or []
            return {
                "revision_count": len(revisions),
                "revision_round": node_update.get("revision_round"),
            }
        if node_name == "wait_human":
            return {
                "human_decision": node_update.get("human_decision"),
                "human_decision_id": node_update.get("human_decision_id"),
            }
        if node_name == "finalize_on_approve":
            return {"terminal": node_update.get("terminal")}
        if node_name == "create_research_backflow_request":
            return {"research_request_id": node_update.get("research_request_id")}
        return {}

    async def _mark_completed(self, run_id: UUID, terminal: str | None) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            await run_repo.mark_completed(run_id, datetime.now(UTC))
            payload: dict = {}
            if terminal is not None:
                payload["terminal"] = terminal
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_COMPLETED.value,
                    stage=TaskStage.EXPORTING.value,
                    progress=100,
                    message="Stage 5 报告控制流完成",
                    payload=payload,
                )
            )
            await session.commit()

    async def _mark_revision_limit_exceeded(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            await run_repo.mark_failed(
                run_id,
                datetime.now(UTC),
                STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED,
                "stage5 revision round limit exceeded",
            )
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_FAILED.value,
                    stage=TaskStage.CHECKING.value,
                    message="Stage 5 修订轮次超限，工作流失败",
                    payload={"terminal": STAGE5_TERMINAL_REVISION_LIMIT_EXCEEDED},
                )
            )
            await session.commit()

    async def _mark_cancelled(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            await run_repo.mark_cancelled(run_id, datetime.now(UTC))
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_CANCELLED.value,
                    stage=TaskStage.AUDITING.value,
                    message="Stage 5 工作流已取消（人工 cancel）",
                    payload={"terminal": STAGE5_TERMINAL_CANCELLED},
                )
            )
            await session.commit()

    async def _mark_waiting_human(self, run_id: UUID) -> None:
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            updated = await run_repo.mark_waiting_human(run_id, "human_review")
            if updated is None:
                return
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_WAITING_HUMAN.value,
                    stage=TaskStage.AUDITING.value,
                    progress=None,
                    message="等待人工裁决（approve / rewrite / research / cancel）",
                    payload={"pending_action": "human_review"},
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
                    stage=TaskStage.WRITING.value,
                    message="Stage 5 报告控制流失败",
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
