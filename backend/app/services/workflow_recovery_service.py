"""Best-effort reconciliation of orphaned workflow runs on backend startup."""

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.logging import get_logger
from app.db.models.workflow_event import WorkflowEventModel
from app.domain.tasks import WorkflowEventType
from app.repositories.workflow_event_repository import WorkflowEventRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository

logger = get_logger("app.recovery")

_ERROR_CODE = "worker_restarted"
_ERROR_MESSAGE = "后台进程重启，原运行已中断"


class ReconciliationResult:
    def __init__(self, marked_failed: int) -> None:
        self.marked_failed = marked_failed


class WorkflowRecoveryService:
    """Single-instance startup strategy: fail orphaned pending/running runs."""

    def __init__(self, sessionmaker: async_sessionmaker) -> None:
        self._sessionmaker = sessionmaker

    async def reconcile_orphaned_runs(self) -> ReconciliationResult:
        marked = 0
        async with self._sessionmaker() as session:
            run_repo = WorkflowRunRepository(session)
            event_repo = WorkflowEventRepository(session)
            orphaned = await run_repo.mark_orphaned_failed(
                datetime.now(UTC),
                _ERROR_CODE,
                _ERROR_MESSAGE,
            )
            for run in orphaned:
                await event_repo.create(
                    WorkflowEventModel(
                        run_id=run.run_id,
                        event_type=WorkflowEventType.RUN_FAILED.value,
                        message=_ERROR_MESSAGE,
                        payload={"error_code": _ERROR_CODE},
                    )
                )
                marked += 1
            await session.commit()
        logger.info("orphaned_runs_reconciled", marked_failed=marked)
        return ReconciliationResult(marked_failed=marked)
