"""Top-level research execution service (Stage 6A spec C).

把 ResearchTask + 显式 Stage 4 work plan 串联为完整研究执行：
    Stage4WorkflowRun → SynthesisResult → Stage5WorkflowRun
并维护 in-process asyncio 后台链（镜像 WorkflowExecutionManager 的单进程
scheduler 模式；**不是**分布式任务队列）。

关键约束（spec C / D）：
- **不重新实现 Stage 2 source planning**：research_question 派生自
  task.questions[0]、analysis_as_of 派生自 task.research_end_date，work plan
  由调用方显式提供；不假装自动 source planning 已完成；
- active-run 不变式：Stage4/Stage5 runner 内部 `get_active_for_task` +
  partial unique index 兜底，同一 task 同时只能有一个 active run（重复启动 →
  `ActiveWorkflowRunExists` 409）；
- 短事务模式：create/claim/event/finalize 各自短 session，graph 运行期间
  不持有 DB session（由 runner 保证）；
- 本轮 0 real LLM：runner 由注入的 factory 构建（测试用 Fake deps）。
"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    MissingResearchQuestion,
    ResearchExecutionRequiresSingleQuestion,
    TaskNotFound,
    WorkflowActionInvalid,
    WorkflowRunAlreadyFinished,
    WorkflowRunNotFound,
)
from app.core.logging import get_logger
from app.db.models.workflow_event import WorkflowEventModel
from app.db.models.workflow_run import WorkflowRunModel
from app.domain.tasks import WorkflowEventType, WorkflowRunStatus
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.review.errors import ReviewError
from app.schemas.research_execution import ResearchExecutionRequest
from app.schemas.workflow import WorkflowRunResponse
from app.services.company_identity_service import CompanyIdentityService
from app.stage4.contracts import Stage4WorkflowRequest
from app.stage5.contracts import Stage5RequestBuilder, Stage5WorkflowRequest
from app.stage5.errors import Stage5WorkflowError
from app.workflows.checkpoint import LangGraphCheckpointManager

logger = get_logger("app.research_execution")


class ResearchExecutionService:
    """单进程研究执行调度器；后台链失败时 runner 已标记对应 run 终态。"""

    def __init__(
        self,
        *,
        sessionmaker: async_sessionmaker,
        checkpoint_manager: LangGraphCheckpointManager,
        company_identity: CompanyIdentityService,
        stage4_runner_factory,
        stage5_runner_factory,
        shutdown_timeout_seconds: int = 10,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._checkpoint_manager = checkpoint_manager
        self._company_identity = company_identity
        self._stage4_runner_factory = stage4_runner_factory
        self._stage5_runner_factory = stage5_runner_factory
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._tasks: dict[UUID, asyncio.Task] = {}
        self._chain_state: dict[UUID, dict] = {}
        self._closed = False

    @property
    def stage4_runner_factory(self):
        """只读暴露惰性 Stage 4 runner factory（artifact workspace 读 checkpoint 用）。"""
        return self._stage4_runner_factory

    @property
    def stage5_runner_factory(self):
        """只读暴露惰性 Stage 5 runner factory（artifact workspace 读 checkpoint 用）。"""
        return self._stage5_runner_factory

    # ------------------------------------------------------------------ start

    async def start(
        self,
        task_id: UUID,
        request: ResearchExecutionRequest,
    ) -> WorkflowRunResponse:
        """启动一次真实研究执行，返回 Stage 4 run（202 语义）。

        1. 校验任务存在（`TaskNotFound`）；
        2. 解析公司身份（`CompanyIdentityNotFound` / `CompanyIdentityAmbiguous`）；
        3. 派生 research_question（task.questions[0]，空 → `MissingResearchQuestion`）
           与 analysis_as_of（task.research_end_date）；
        4. `create_stage4_run`（active-run 不变式 → `ActiveWorkflowRunExists` 409）；
        5. 调度后台链 Stage4 → SynthesisResult → Stage5，立即返回 Stage 4 run。
        """
        if self._closed:
            raise RuntimeError("research execution service is closed")
        async with self._sessionmaker() as session:
            task = await ResearchTaskRepository(session).get_by_id(task_id)
        if task is None:
            raise TaskNotFound()

        questions = list(task.questions or [])
        if len(questions) > 1:
            # 不能静默只取第一条：多问题编排尚未实现，明确 422 拒绝。
            raise ResearchExecutionRequiresSingleQuestion()
        research_question = questions[0] if questions else None
        if not research_question:
            raise MissingResearchQuestion()
        analysis_as_of = task.research_end_date

        resolution = await self._company_identity.resolve(task.company_query)
        company_id = resolution.company.company_id

        stage4_request = Stage4WorkflowRequest(
            task_id=task_id,
            company_id=company_id,
            research_question=research_question,
            analysis_as_of=analysis_as_of,
            analysis_work_items=list(request.analysis_work_items),
        )
        stage4_runner = self._stage4_runner_factory()
        run = await stage4_runner.create_stage4_run(stage4_request)

        self._chain_state[task_id] = {
            "stage4_run_id": run.run_id,
            "stage4_request": stage4_request,
        }
        self._schedule(task_id, self._execute_chain(task_id))
        return run

    # ------------------------------------------------------------------ chain

    async def _execute_chain(self, task_id: UUID) -> None:
        """Stage4 execute → SynthesisResult → Stage5 create+execute。

        各 runner 自行短事务 finalize；本方法只负责顺序编排与日志，不持有
        DB session。Stage 5 到达 WAITING_HUMAN 时链自然结束（graph interrupt），
        human action 走 `resume_human`。
        """
        try:
            state = self._chain_state.get(task_id)
            if state is None:
                return
            stage4_request: Stage4WorkflowRequest = state["stage4_request"]
            stage4_runner = self._stage4_runner_factory()
            result = await stage4_runner.execute_stage4(state["stage4_run_id"], stage4_request)

            synthesis_result_id = result.get("synthesis_result_id")
            if not synthesis_result_id:
                logger.warning(
                    "stage4_no_synthesis_result",
                    task_id=str(task_id),
                    stage4_run_id=str(state["stage4_run_id"]),
                )
                return

            stage5_request = Stage5RequestBuilder.from_stage4_state(
                task_id=task_id,
                stage4_state=result,
                synthesis_result_id=UUID(synthesis_result_id),
            )
            await self._continue_to_stage5(task_id, stage5_request)
        except Exception as exc:
            # runner 已标记对应 run 终态（fail/cancel）；这里只记录，不重抛
            # 到调用方（后台任务异常由 _on_task_done 消费）。
            logger.warning(
                "research_chain_failed",
                task_id=str(task_id),
                error_type=type(exc).__name__,
            )
        finally:
            self._chain_state.pop(task_id, None)

    async def _continue_to_stage5(
        self,
        task_id: UUID,
        stage5_request: Stage5WorkflowRequest,
    ) -> None:
        """Stage 4 完成后的 Stage 5 续接：create → execute。

        首启（`start`）与恢复（`_recover_chain`）共用；调用前
        `_chain_state[task_id]` 必须已存在（记录 stage5_run_id）。
        """
        stage5_runner = self._stage5_runner_factory()
        stage5_run = await stage5_runner.create_stage5_run(stage5_request)
        self._chain_state[task_id]["stage5_run_id"] = stage5_run.run_id
        await stage5_runner.execute_stage5(stage5_run.run_id, stage5_request)

    # ------------------------------------------------------------------ recovery

    async def _recover_stage5_chain(self, task_id: UUID) -> None:
        """Stage 5 worker 重启恢复：同 run/thread 从最后 checkpoint 续跑。

        runner 自行 claim(worker_restarted) + 短事务 finalize；本方法只负责
        顺序编排与日志，不持有 DB session。Stage 5 恢复后到达 WAITING_HUMAN
        时链自然结束（graph interrupt），人工 action 走 `resume_human`。
        """
        try:
            state = self._chain_state.get(task_id)
            if state is None:
                return
            runner = self._stage5_runner_factory()
            await runner.resume_stage5_for_recovery(state["stage5_run_id"])
        except Exception as exc:
            logger.warning(
                "research_stage5_recovery_failed",
                task_id=str(task_id),
                error_type=type(exc).__name__,
            )
        finally:
            self._chain_state.pop(task_id, None)

    async def _recover_chain(self, task_id: UUID) -> None:
        """启动恢复路径（spec E）：Stage 4 durable → Stage 5 续接。

        - `resume_stage4=True`（run FAILED(worker_restarted)）：同 run/thread
          从 checkpoint 恢复 Stage 4（synthesis 幂等 → 无重复产物）；
        - 否则（run COMPLETED）：直接读 checkpoint 的 synthesis_result_id，
          不再重跑 Stage 4。
        两者都投影出 Stage5WorkflowRequest 后走 `_continue_to_stage5`。
        """
        try:
            state = self._chain_state.get(task_id)
            if state is None:
                return
            stage4_runner = self._stage4_runner_factory()
            if state.get("resume_stage4"):
                result = await stage4_runner.resume_stage4(state["stage4_run_id"])
            else:
                result = await stage4_runner.read_checkpoint_state(state["stage4_run_id"])
            synthesis_result_id = result.get("synthesis_result_id")
            if not synthesis_result_id:
                logger.warning(
                    "stage4_recovery_no_synthesis_result",
                    task_id=str(task_id),
                    stage4_run_id=str(state["stage4_run_id"]),
                )
                return
            stage5_request = Stage5RequestBuilder.from_stage4_state(
                task_id=task_id,
                stage4_state=result,
                synthesis_result_id=UUID(synthesis_result_id),
            )
            await self._continue_to_stage5(task_id, stage5_request)
        except Exception as exc:
            logger.warning(
                "research_chain_recovery_failed",
                task_id=str(task_id),
                error_type=type(exc).__name__,
            )
        finally:
            self._chain_state.pop(task_id, None)

    def schedule_recovery(
        self,
        task_id: UUID,
        stage4_run_id: UUID,
        *,
        resume_stage4: bool,
    ) -> bool:
        """启动恢复入口（sync）：为已中断 task 调度 Stage 5 续接。

        幂等：该 task 已有后台链或服务已关闭 → 不调度、返回 False。由
        ResearchExecutionRecoveryCoordinator 在 reconcile 之后、服务正式对外前
        调用。
        """
        if self._closed or task_id in self._tasks:
            return False
        self._chain_state[task_id] = {
            "stage4_run_id": stage4_run_id,
            "resume_stage4": resume_stage4,
        }
        self._schedule(task_id, self._recover_chain(task_id))
        return True

    def schedule_stage5_recovery(self, task_id: UUID, stage5_run_id: UUID) -> bool:
        """启动 Stage 5 恢复入口（sync）：为 worker 重启中断的 Stage5 run 调度恢复。

        幂等：该 task 已有后台链或服务已关闭 → 不调度、返回 False。由
        ResearchExecutionRecoveryCoordinator 在 reconcile 之后、服务正式对外前
        调用。恢复复用同 run / thread（`resume_stage5_for_recovery`），不新建
        WorkflowRun。
        """
        if self._closed or task_id in self._tasks:
            return False
        self._chain_state[task_id] = {"stage5_run_id": stage5_run_id}
        self._schedule(task_id, self._recover_stage5_chain(task_id))
        return True

    # ------------------------------------------------------------------ actions

    async def resume_human(
        self,
        run_id: UUID,
        decision: str,
        comment: str | None = None,
    ) -> WorkflowRunResponse:
        """Stage 5 WAITING_HUMAN run 人工裁决（approve/rewrite/research/cancel）。

        委托 Stage5WorkflowRunner.resume_stage5_human；返回裁决后的 run。
        """
        if self._closed:
            raise RuntimeError("research execution service is closed")
        try:
            runner = self._stage5_runner_factory()
            await runner.resume_stage5_human(run_id, decision, comment)
            return await runner.get_run(run_id)
        except (Stage5WorkflowError, ReviewError) as exc:
            raise WorkflowActionInvalid() from exc

    async def cancel(self, run_id: UUID) -> WorkflowRunResponse:
        """取消一次真实研究 run。

        - WAITING_HUMAN：走 graph-aware cancel（resume decision=cancel）→
          终端 CANCELLED；
        - pending/running：取消后台链任务并 `mark_cancelled`（与 Stage 1
          cancel 语义一致）。
        """
        if self._closed:
            raise RuntimeError("research execution service is closed")
        run = await self._get_run(run_id)
        task = self._tasks.get(run.task_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # 竞态：runner 可能已标记 failed；终态由 DB 原子更新裁决
                pass

        if run.status == WorkflowRunStatus.WAITING_HUMAN.value:
            return await self.resume_human(run_id, "cancel", None)

        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            updated = await run_repo.mark_cancelled(run_id, datetime.now(UTC))
            if updated is None:
                raise WorkflowRunAlreadyFinished()
            await event_repo.create(
                WorkflowEventModel(
                    run_id=run_id,
                    event_type=WorkflowEventType.RUN_CANCELLED.value,
                    message="研究执行已取消",
                    payload={},
                )
            )
            await session.commit()
        return WorkflowRunResponse.model_validate(updated)

    def is_running(self, task_id: UUID) -> bool:
        """该 task 是否有仍在执行的后台研究链（task 级 SSE 判 terminal 用）。"""
        task = self._tasks.get(task_id)
        return task is not None and not task.done()

    async def has_persisted_plan(self, run_id: UUID) -> bool:
        """真实 Stage 4 Web retry 前提：是否有完整持久化的 execution request 可重建。

        Stage 6A v1 只在内存 chain state 保存 work plan（进程重启即丢失），
        没有一等公民的持久化 request → 恒为 False；actions 路由据此对
        Stage 4 retry 返回稳定 409 workflow_action_invalid，不假装支持。
        """
        return False

    # ------------------------------------------------------------------ internal

    async def _get_run(self, run_id: UUID) -> WorkflowRunModel:
        async with self._sessionmaker() as session:
            run = await WorkflowRunRepository(session).get_by_id(run_id)
        if run is None:
            raise WorkflowRunNotFound()
        return run

    def _schedule(self, task_id: UUID, coroutine) -> None:
        task = asyncio.create_task(coroutine, name=f"research-{task_id}")
        self._tasks[task_id] = task
        task.add_done_callback(self._on_task_done)

    def _on_task_done(self, task: asyncio.Task) -> None:
        name = task.get_name()
        task_id = UUID(name.removeprefix("research-")) if name.startswith("research-") else None
        if task_id is not None:
            self._tasks.pop(task_id, None)
        try:
            task.exception()  # 消费异常，避免 "Task exception was never retrieved"
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "research_task_failed",
                task_id=str(task_id),
                error_type=type(exc).__name__,
            )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        pending = [task for task in self._tasks.values() if not task.done()]
        if not pending:
            self._tasks.clear()
            self._chain_state.clear()
            return
        done, pending = await asyncio.wait(pending, timeout=self._shutdown_timeout_seconds)
        if pending:
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
        self._tasks.clear()
        self._chain_state.clear()
