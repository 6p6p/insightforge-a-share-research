"""Top-level research orchestration runner (stage 7A.2B.1 spec I/M/N/O + 7A.2B.2 L/M).

跑顶层 orchestration graph，PG Checkpointer、`thread_id = orchestration_id`
（**顶层线程 != child Stage4/Stage5 线程**——child 仍是 `thread_id = run_id`、
独立 checkpoint / recovery / action 语义，spec N）。

- **run_orchestration(orchestration_id)**：checkpoint-aware——已有顶层 checkpoint
  → 恢复；无 checkpoint → 初始 state 首启。graph 期间不持有 DB session；
- **awaiting_stage5 continuation（spec M）**：graph 到 END 暂停（phase=
  awaiting_stage5、Stage5 child WAITING_HUMAN）。人工裁决 child 后再次
  `run_orchestration`：`aget_state().next` 为空（graph 已完成）且 phase 仍为
  awaiting_stage5 → `aupdate_state(as_node=ensure_stage5_child)` 重新进入
  `run_or_resume_stage5` 重新判定 child 状态（completed → complete /
  research_required → pause_for_research / 仍 waiting_human → 再次 pause）。
  **注意 `aupdate_state` 注入的是 fresh 路由值（child 终态由节点重查 DB），
  不是 stale 的 `stage5_run_status`**；
- **失败投影（spec M）**：graph 抛异常 → orchestration `status=failed`、phase 保持
  stage4/stage5（child 阶段失败时）`error_code` 用稳定投影（stage4_execution_failed /
  stage5_execution_failed / orchestration_execution_failed），**不吞 child 错误**
  （child 自身 run 的 error_code/message 已由 Stage4/5 runner 写在 WorkflowRun 行上）；
- `awaiting_stage5` **不是** orchestration completed（status=waiting_human，
  等 Stage5 人工裁决）。
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.research_backflow.errors import ResearchBackflowNoProgress
from app.research_orchestration.contracts import (
    RESEARCH_BACKFLOW_NO_PROGRESS,
    OrchestrationPhase,
    OrchestrationStatus,
)
from app.research_orchestration.dependencies import ResearchOrchestrationDependencies
from app.research_orchestration.errors import (
    ResearchOrchestrationAlreadyFinished,
    ResearchOrchestrationNotFound,
)
from app.research_orchestration.graph import (
    build_top_level_research_orchestration_graph,
)
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.workflows.checkpoint import LangGraphCheckpointManager

_TERMINAL_ORCHESTRATION_STATUSES = frozenset(
    {
        OrchestrationStatus.COMPLETED.value,
        OrchestrationStatus.FAILED.value,
        OrchestrationStatus.CANCELLED.value,
    }
)
_MAX_ERROR_MESSAGE_LENGTH = 200


def _sanitize_error(exc: Exception) -> str:
    """稳定投影：只保留异常类型名（不泄漏 SQL / stack / raw message）。"""
    return type(exc).__name__[:_MAX_ERROR_MESSAGE_LENGTH]


class ResearchOrchestrationRunner:
    """执行 / 恢复顶层 orchestration graph。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
        dependencies: ResearchOrchestrationDependencies,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._checkpoint_manager = checkpoint_manager
        self._dependencies = dependencies

    # ------------------------------------------------------------------ run

    async def run_orchestration(self, orchestration_id: UUID) -> dict:
        """首启或恢复一次 orchestration（checkpoint-aware）。

        - orchestration 已 terminal → `ResearchOrchestrationAlreadyFinished`；
        - 已有顶层 checkpoint → resume（同 orchestration_id + 同顶层 thread，
          spec O：**绝不新建 orchestration / 绝不换 thread**）；
        - 无 checkpoint → 初始 state 首启（planning）。
        """
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
        if orchestration is None:
            raise ResearchOrchestrationNotFound()
        if orchestration.status in _TERMINAL_ORCHESTRATION_STATUSES:
            raise ResearchOrchestrationAlreadyFinished()

        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_top_level_research_orchestration_graph(self._dependencies, checkpointer)
        config = {"configurable": {"thread_id": str(orchestration_id)}}

        prior = await graph.aget_state(config)
        if prior is not None and prior.values:
            phase = prior.values.get("current_phase") or orchestration.current_phase
            await self._mark_running(orchestration_id, phase=phase)
            if not prior.next and phase == OrchestrationPhase.AWAITING_STAGE5.value:
                # **awaiting_stage5 continuation（spec M）**：顶层 graph 已到 END
                # （Stage5 child 人工裁决后）。`aupdate_state(as_node=
                # ensure_stage5_child)` 把 next 重新指向 run_or_resume_stage5——
                # 节点重查 child 终态、注入 fresh `stage5_run_status`，条件边重新
                # 路由（completed → complete / research_required → pause /
                # 仍 waiting_human → 再次 pause）。
                await graph.aupdate_state(
                    config,
                    {"current_phase": OrchestrationPhase.STAGE5.value},
                    as_node="ensure_stage5_child",
                )
            final_state = await self._stream(graph, config, initial_state=None)
        else:
            await self._mark_running(orchestration_id, phase=OrchestrationPhase.PLANNING.value)
            initial_state = {
                "orchestration_id": str(orchestration_id),
                "task_id": str(orchestration.task_id),
                "research_plan_id": (
                    str(orchestration.research_plan_id)
                    if orchestration.research_plan_id is not None
                    else ""
                ),
                "current_phase": OrchestrationPhase.PLANNING.value,
            }
            final_state = await self._stream(graph, config, initial_state=initial_state)

        if final_state is not None and not final_state.values:
            return {}
        return dict(final_state.values) if final_state is not None else {}

    # ------------------------------------------------------------------ read

    async def read_orchestration_checkpoint(self, orchestration_id: UUID) -> dict:
        """只读读取顶层 checkpoint state（恢复协调器判定 phase 用）。

        不 claim、不改 orchestration 状态；无 checkpoint → 空 dict。
        """
        checkpointer = await self._checkpoint_manager.get_checkpointer()
        graph = build_top_level_research_orchestration_graph(self._dependencies, checkpointer)
        state = await graph.aget_state({"configurable": {"thread_id": str(orchestration_id)}})
        return dict(state.values) if state is not None else {}

    # ------------------------------------------------------------------ internal

    async def _stream(self, graph, config, *, initial_state: dict | None) -> dict:
        """共享执行路径：graph 期间不持有 DB session；异常 → 失败投影后重抛。

        `ResearchBackflowNoProgress`（7A.2B.3 兜底）：fulfill_request 的 no-progress
        政策（新 SynthesisResult 与 source 相同）触发 → **不投影 failed**，而是
        backflow manual_required（status=waiting_human、phase=research_backflow、
        reason=research_backflow_no_progress；verify_progress 已拦截无新证据，
        此处是 S2 兜底）。
        """
        try:
            async for _update in graph.astream(initial_state, config, stream_mode="updates"):
                pass
            return await graph.aget_state(config)
        except asyncio.CancelledError:
            raise
        except ResearchBackflowNoProgress:
            thread_id = UUID(config["configurable"]["thread_id"])
            await self._mark_research_backflow_manual(thread_id)
            await graph.aupdate_state(
                config,
                {"backflow_manual_reason": RESEARCH_BACKFLOW_NO_PROGRESS},
                as_node="research_backflow_manual",
            )
            return await graph.aget_state(config)
        except Exception as exc:
            await self._mark_orchestration_failed(UUID(config["configurable"]["thread_id"]), exc)
            raise

    async def _mark_research_backflow_manual(self, orchestration_id: UUID) -> None:
        """backflow 终止投影：status=waiting_human、phase=research_backflow。"""
        async with self._sessionmaker() as session:
            await ResearchOrchestrationRepository(session).update_progress(
                orchestration_id,
                status=OrchestrationStatus.WAITING_HUMAN.value,
                current_phase=OrchestrationPhase.RESEARCH_BACKFLOW.value,
            )
            await session.commit()

    async def _mark_running(self, orchestration_id: UUID, *, phase: str) -> None:
        async with self._sessionmaker() as session:
            await ResearchOrchestrationRepository(session).update_progress(
                orchestration_id,
                status=OrchestrationStatus.RUNNING.value,
                current_phase=phase,
            )
            await session.commit()

    async def _mark_orchestration_failed(self, orchestration_id: UUID, exc: Exception) -> None:
        """稳定失败投影（spec M）：phase 保持 stage4/stage5（child 阶段失败）或当前 phase。"""
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(session).get_by_id(
                orchestration_id
            )
            phase = (
                orchestration.current_phase
                if orchestration is not None
                else OrchestrationPhase.PLANNING.value
            )
            error_code = (
                "stage4_execution_failed"
                if phase == OrchestrationPhase.STAGE4.value
                else "stage5_execution_failed"
                if phase == OrchestrationPhase.STAGE5.value
                else "orchestration_execution_failed"
            )
            await ResearchOrchestrationRepository(session).mark_failed(
                orchestration_id,
                datetime.now(UTC),
                error_code=error_code,
                error_message=_sanitize_error(exc),
            )
            await session.commit()
