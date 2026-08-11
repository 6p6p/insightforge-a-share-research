"""Task workspace projection service (Stage 6A spec E / Stage 6B.1).

把 ResearchTask + 解析公司 + 当前 run + **任务级**证据链产物计数 组装成
`TaskWorkspaceResponse`，供 Web 工作台一次性渲染。只做只读投影，不修改业务数据。

Stage 6B.1 起计数改为任务级：注入 `TaskArtifactService` 时按任务从 checkpoint
精确恢复 ID 集合计数（source/evidence/claim/report/review issue）。未注入时保留
公司级 `_count_artifacts` 作兼容回退（deprecated，仅直接构造实例的场景）。
"""

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.core.errors import (
    CompanyIdentityAmbiguous,
    CompanyIdentityNotFound,
    TaskNotFound,
)
from app.repositories.research_task_repository import ResearchTaskRepository
from app.repositories.workflow_run_repository import WorkflowRunRepository
from app.schemas.research_execution import ArtifactSummary, TaskWorkspaceResponse
from app.schemas.task import TaskResponse
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_service import ResearchExecutionService
from app.services.task_artifact_service import TaskArtifactService


class TaskWorkspaceService:
    """只读 workspace 投影；每个方法使用短生命周期 session。"""

    def __init__(
        self,
        sessionmaker: async_sessionmaker,
        research_execution: ResearchExecutionService | None = None,
        artifact_service: TaskArtifactService | None = None,
    ) -> None:
        self._sessionmaker = sessionmaker
        self._company_identity = CompanyIdentityService(sessionmaker)
        self._research_execution = research_execution
        self._artifact_service = artifact_service

    async def get_workspace(self, task_id: UUID) -> TaskWorkspaceResponse:
        async with self._sessionmaker() as session:
            task_model = await ResearchTaskRepository(session).get_by_id(task_id)
            run_model = await WorkflowRunRepository(session).get_latest_for_task(task_id)
        if task_model is None:
            raise TaskNotFound()
        task = TaskResponse.model_validate(task_model)

        resolved_company = None
        company_id: UUID | None = None
        try:
            resolution = await self._company_identity.resolve(task.company_query)
            resolved_company = resolution.company
            company_id = resolution.company.company_id
        except (CompanyIdentityNotFound, CompanyIdentityAmbiguous):
            # 公司身份未解析时仍返回 workspace，只是无计数与公司信息。
            resolved_company = None

        current_run = None
        if run_model is not None:
            from app.schemas.workflow import WorkflowRunResponse

            current_run = WorkflowRunResponse.model_validate(run_model)

        if self._artifact_service is not None:
            summary = await self._artifact_service.count_artifacts(task_id)
        else:
            summary = await self._count_artifacts(company_id)
        research_chain_active = False
        if self._research_execution is not None:
            # 后台研究链（Stage4→Stage5 过渡）仍在执行 → task 尚非真正终态，
            # task 级 SSE 客户端据此暂不关闭事件流（spec D）。
            research_chain_active = self._research_execution.is_running(task_id)
        return TaskWorkspaceResponse(
            task=task,
            resolved_company=resolved_company,
            current_run=current_run,
            artifact_summary=summary,
            research_chain_active=research_chain_active,
        )

    async def _count_artifacts(self, company_id: UUID | None) -> ArtifactSummary:
        # Deprecated：Stage 6B.1 起注入 artifact_service 走任务级计数；此处仅作
        # 兼容回退（直接构造实例而未注入时），语义仍是公司级全集。
        """（deprecated）公司级产物计数回退；生产路径已由任务级计数取代。"""
        if company_id is None:
            return ArtifactSummary()
        async with self._sessionmaker() as session:
            source_count = await self._count(session, "source_records", company_id, None)
            evidence_count = await self._count(session, "evidence_cards", company_id, None)
            claim_count = await self._count(session, "claims", company_id, None)
            report_count = await self._count(session, "reports", company_id, None)
            review_issue_count = await self._count(
                session,
                "review_issues",
                company_id,
                "ri JOIN report_audits a ON ri.audit_id = a.audit_id "
                "JOIN reports r ON a.report_id = r.report_id",
            )
        return ArtifactSummary(
            source_count=source_count,
            evidence_count=evidence_count,
            claim_count=claim_count,
            report_count=report_count,
            review_issue_count=review_issue_count,
        )

    @staticmethod
    async def _count(session, table: str, company_id: UUID, alias: str | None) -> int:
        """按 company_id 计数一张表；`alias` 传入时对别名表做 join（如 review_issues）。"""
        if alias is not None:
            sql = f"SELECT count(*) FROM {table} {alias} WHERE r.company_id = :cid"
        else:
            sql = f"SELECT count(*) FROM {table} WHERE company_id = :cid"
        result = await session.execute(text(sql).bindparams(cid=company_id))
        return int(result.scalar_one())
