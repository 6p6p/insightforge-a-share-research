"""Business logic for research task creation and queries."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import IdempotencyConflict, TaskNotFound
from app.db.models.research_task import ResearchTaskModel
from app.domain.tasks import TaskStage, TaskStatus
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_orchestration.repository import ResearchOrchestrationRepository
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse
from app.services.task_status_projection import (
    PUBLIC_STATUS_NOT_STARTED,
    project_public_status,
)

_UNIQUE_VIOLATION = "23505"
_IDEMPOTENCY_CONSTRAINT = "uq_research_tasks_idempotency_key"


@dataclass
class TaskCreationResult:
    task: TaskResponse
    replayed: bool


class TaskService:
    def __init__(
        self,
        repository: ResearchTaskRepository,
        sessionmaker: async_sessionmaker | None = None,
    ) -> None:
        self._repository = repository
        # 可选：用于 canonical public status projection（task + 最新 orchestration）。
        # 未注入（unit 测试 / 内部短生命周期调用）→ public_status 只按 task 自身推导。
        self._sessionmaker = sessionmaker

    async def create_task(
        self,
        request: TaskCreateRequest,
        idempotency_key: str | None,
    ) -> TaskCreationResult:
        fingerprint = self._fingerprint(request)
        if idempotency_key is not None:
            existing = await self._repository.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return await self._replay_or_conflict(existing, fingerprint)

        task = self._build_model(request, idempotency_key, fingerprint)
        try:
            await self._repository.create(task)
        except IntegrityError as exc:
            if not self._is_idempotency_conflict(exc):
                raise
            await self._repository.session.rollback()
            existing = await self._repository.get_by_idempotency_key(idempotency_key)
            if existing is None:
                raise
            return await self._replay_or_conflict(existing, fingerprint)
        # 新任务尚无 orchestration → 未开始（canonical projection 单点推导）。
        return TaskCreationResult(
            task=self._to_response(task, public_status=PUBLIC_STATUS_NOT_STARTED),
            replayed=False,
        )

    async def get_task(self, task_id: UUID) -> TaskResponse:
        task = await self._repository.get_by_id(task_id)
        if task is None:
            raise TaskNotFound()
        public_status = await self._project_public_status(task)
        return self._to_response(task, public_status=public_status)

    async def list_tasks(
        self,
        status: TaskStatus | None,
        limit: int,
        offset: int,
    ) -> TaskListResponse:
        rows, total = await self._repository.list_tasks(
            status=status,
            limit=limit,
            offset=offset,
        )
        statuses = await self._project_public_statuses(rows)
        items = [self._to_response(task, public_status=statuses[task.task_id]) for task in rows]
        return TaskListResponse(items=items, total=total, limit=limit, offset=offset)

    @staticmethod
    def _fingerprint(request: TaskCreateRequest) -> str:
        payload = json.dumps(
            request.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _build_model(
        request: TaskCreateRequest,
        idempotency_key: str | None,
        fingerprint: str,
    ) -> ResearchTaskModel:
        return ResearchTaskModel(
            company_query=request.company_query,
            research_start_date=request.research_start_date,
            research_end_date=request.research_end_date,
            modules=[module.value for module in request.modules],
            questions=request.questions,
            include_relative_valuation=request.include_relative_valuation,
            require_plan_approval=request.require_plan_approval,
            status=TaskStatus.PENDING.value,
            current_stage=TaskStage.CREATED.value,
            progress=0,
            idempotency_key=idempotency_key,
            # 幂等对只在有 Idempotency-Key 时生效；无 key 时两个字段都置空，
            # 满足 ck_research_tasks_idempotency_pair。
            request_fingerprint=fingerprint if idempotency_key is not None else None,
        )

    @staticmethod
    def _to_response(task: ResearchTaskModel, public_status: str) -> TaskResponse:
        base = TaskResponse.model_validate(task)
        return base.model_copy(update={"public_status": public_status})

    @staticmethod
    def _is_idempotency_conflict(exc: IntegrityError) -> bool:
        diag = getattr(exc.orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None)
        constraint = getattr(diag, "constraint_name", None)
        return sqlstate == _UNIQUE_VIOLATION and constraint == _IDEMPOTENCY_CONSTRAINT

    async def _replay_or_conflict(
        self,
        existing: ResearchTaskModel,
        fingerprint: str,
    ) -> TaskCreationResult:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict()
        public_status = await self._project_public_status(existing)
        return TaskCreationResult(
            task=self._to_response(existing, public_status=public_status),
            replayed=True,
        )

    # ------------------------------------------------------------ projection

    async def _project_public_status(self, task: ResearchTaskModel) -> str:
        """task + 最新 orchestration → canonical public status（单点权威）。

        sessionmaker 未注入 → 只按 task 自身推导（不查 orchestration）。
        """
        if self._sessionmaker is None:
            return project_public_status(task_status=task.status)
        async with self._sessionmaker() as session:
            orchestration = await ResearchOrchestrationRepository(
                session
            ).get_latest_for_task(task.task_id)
        return project_public_status(
            task_status=task.status,
            orchestration_status=(
                orchestration.status if orchestration is not None else None
            ),
        )

    async def _project_public_statuses(
        self, tasks: list[ResearchTaskModel]
    ) -> dict[UUID, str]:
        """批量投影（列表页避免 N+1）；无 orchestration 信息 → 按 task 自身推导。"""
        if self._sessionmaker is None or not tasks:
            return {
                task.task_id: project_public_status(task_status=task.status) for task in tasks
            }
        task_ids = [task.task_id for task in tasks]
        async with self._sessionmaker() as session:
            latest = await ResearchOrchestrationRepository(session).list_latest_for_tasks(
                task_ids
            )
        return {
            task.task_id: project_public_status(
                task_status=task.status,
                orchestration_status=(
                    latest[task.task_id].status if task.task_id in latest else None
                ),
            )
            for task in tasks
        }
