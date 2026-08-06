"""Business logic for research task creation and queries."""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from app.core.errors import IdempotencyConflict, TaskNotFound
from app.db.models.research_task import ResearchTaskModel
from app.domain.tasks import TaskStage, TaskStatus
from app.repositories.research_task_repository import ResearchTaskRepository
from app.schemas.task import TaskCreateRequest, TaskListResponse, TaskResponse

_UNIQUE_VIOLATION = "23505"
_IDEMPOTENCY_CONSTRAINT = "uq_research_tasks_idempotency_key"


@dataclass
class TaskCreationResult:
    task: TaskResponse
    replayed: bool


class TaskService:
    def __init__(self, repository: ResearchTaskRepository) -> None:
        self._repository = repository

    async def create_task(
        self,
        request: TaskCreateRequest,
        idempotency_key: str | None,
    ) -> TaskCreationResult:
        fingerprint = self._fingerprint(request)
        if idempotency_key is not None:
            existing = await self._repository.get_by_idempotency_key(idempotency_key)
            if existing is not None:
                return self._replay_or_conflict(existing, fingerprint)

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
            return self._replay_or_conflict(existing, fingerprint)
        return TaskCreationResult(task=self._to_response(task), replayed=False)

    async def get_task(self, task_id: UUID) -> TaskResponse:
        task = await self._repository.get_by_id(task_id)
        if task is None:
            raise TaskNotFound()
        return self._to_response(task)

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
        items = [self._to_response(task) for task in rows]
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
    def _to_response(task: ResearchTaskModel) -> TaskResponse:
        return TaskResponse.model_validate(task)

    @staticmethod
    def _is_idempotency_conflict(exc: IntegrityError) -> bool:
        diag = getattr(exc.orig, "diag", None)
        sqlstate = getattr(diag, "sqlstate", None)
        constraint = getattr(diag, "constraint_name", None)
        return sqlstate == _UNIQUE_VIOLATION and constraint == _IDEMPOTENCY_CONSTRAINT

    @staticmethod
    def _replay_or_conflict(
        existing: ResearchTaskModel,
        fingerprint: str,
    ) -> TaskCreationResult:
        if existing.request_fingerprint != fingerprint:
            raise IdempotencyConflict()
        return TaskCreationResult(task=TaskResponse.model_validate(existing), replayed=True)
