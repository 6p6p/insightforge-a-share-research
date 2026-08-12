"""Top-level research orchestration runner (stage 7A.2B.1 spec I/M/N/O).

跑顶层 orchestration graph，PG Checkpointer、`thread_id = orchestration_id`
（**顶层线程 != child Stage4/Stage5 线程**——child 仍是 `thread_id = run_id`、
独立 checkpoint / recovery / action 语义，spec N）。

- **run_orchestration(orchestration_id)**：checkpoint-aware——已有顶层 checkpoint
  → `graph.astream(None, ...)` 从 checkpoint 恢复；无 checkpoint → 初始 state
  首启。graph 期间不持有 DB session；
- **失败投影（spec M）**：graph 抛异常 → orchestration `status=failed`、phase 保持
  stage4（child 阶段失败时）`error_code` 用稳定投影（stage4_execution_failed /
  orchestration_execution_failed），**不吞 child 错误**（child 自身 run 的
  error_code/message 已由 Stage4 runner 写在 WorkflowRun 行上）；
- `awaiting_stage5` 是 7A.2B.1 正常 terminal phase（status 保持 running，
  等 7A.2B.2 接 Stage5），**不是 orchestration completed**。
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.research_orchestration.contracts import (
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
        """共享执行路径：graph 期间不持有 DB session；异常 → 失败投影后重抛。"""
        try:
            async for _update in graph.astream(initial_state, config, stream_mode="updates"):
                pass
            return await graph.aget_state(config)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._mark_orchestration_failed(UUID(config["configurable"]["thread_id"]), exc)
            raise

    async def _mark_running(self, orchestration_id: UUID, *, phase: str) -> None:
        async with self._sessionmaker() as session:
            await ResearchOrchestrationRepository(session).update_progress(
                orchestration_id,
                status=OrchestrationStatus.RUNNING.value,
                current_phase=phase,
            )
            await session.commit()

    async def _mark_orchestration_failed(self, orchestration_id: UUID, exc: Exception) -> None:
        """稳定失败投影（spec M）：phase 保持 stage4（child 阶段失败）或当前 phase。"""
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
                else "orchestration_execution_failed"
            )
            await ResearchOrchestrationRepository(session).mark_failed(
                orchestration_id,
                datetime.now(UTC),
                error_code=error_code,
                error_message=_sanitize_error(exc),
            )
            await session.commit()
