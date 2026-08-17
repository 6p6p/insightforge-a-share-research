"""FastAPI dependency wiring for task and workflow services."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dependencies import get_db_session
from app.report_export.service import ReportExportService
from app.repositories.research_task_repository import ResearchTaskRepository
from app.research_orchestration.service import ResearchOrchestrationService
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_service import ResearchExecutionService
from app.services.source_ingestion_service import SourceIngestionService
from app.services.source_preparation_service import SourcePreparationService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_artifact_service import TaskArtifactService
from app.services.task_citation_service import TaskCitationService
from app.services.task_service import TaskService
from app.services.task_workspace_service import TaskWorkspaceService
from app.services.workflow_service import WorkflowService
from app.stage5.dependencies import create_stage5_dependencies
from app.storage.export_store import ExportArtifactStore
from app.storage.raw_store import LocalRawArtifactStore
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager


def get_task_service(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TaskService:
    repository = ResearchTaskRepository(session)
    # 注入 sessionmaker：list/get 时按 task + 最新 orchestration 推导 canonical
    # public_status（Product Consistency；见 task_status_projection）。
    from app.db.dependencies import get_database

    database = get_database(request)
    return TaskService(repository, database.session_factory())


def get_workflow_execution_manager(request: Request) -> WorkflowExecutionManager:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.workflow_execution is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.workflow_execution


def get_research_execution_service(request: Request) -> ResearchExecutionService:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.research_execution is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.research_execution


def get_research_orchestration_service(request: Request) -> ResearchOrchestrationService:
    """顶层编排应用服务（7A.2B.2 spec U）：lifespan 装配的同一实例（绑定
    stage5_runner + orchestration_runner），API 只做协议层 dispatch。"""
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.research_orchestration is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.research_orchestration


def get_task_artifact_service(request: Request) -> TaskArtifactService:
    """任务级 artifact workspace（Stage 6B.1）。

    复用 `create_stage5_dependencies` 装配的同一批 Services（verify 链共享），
    经 `from_dependencies` 注入 TaskArtifactService——只读路径 **0 LLM**，不依赖
    DEEPSEEK_API_KEY。checkpoint 读取用裸 `LangGraphCheckpointManager`。
    """
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None or resources.langgraph is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    settings = request.app.state.settings
    sessionmaker = resources.database.session_factory()
    deps = create_stage5_dependencies(settings, sessionmaker)
    return TaskArtifactService.from_dependencies(sessionmaker, resources.langgraph, deps)


def get_task_citation_service(request: Request) -> TaskCitationService:
    """任务级 citation navigation（Stage 6B.2）。

    复用 `create_stage5_dependencies` + TaskArtifactService（canonical lineage
    scope 判定），Evidence / Claim / Source provenance 走
    `EvidenceProvenanceService` verified 链——只读路径 **0 LLM**，不依赖
    DEEPSEEK_API_KEY。
    """
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None or resources.langgraph is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    settings = request.app.state.settings
    sessionmaker = resources.database.session_factory()
    deps = create_stage5_dependencies(settings, sessionmaker)
    artifact_service = TaskArtifactService.from_dependencies(
        sessionmaker, resources.langgraph, deps
    )
    return TaskCitationService(sessionmaker, artifact_service)


def get_task_workspace_service(
    request: Request,
    research_execution: Annotated[
        ResearchExecutionService, Depends(get_research_execution_service)
    ],
) -> TaskWorkspaceService:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None or resources.langgraph is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    sessionmaker = resources.database.session_factory()
    settings = request.app.state.settings
    deps = create_stage5_dependencies(settings, sessionmaker)
    artifact_service = TaskArtifactService.from_dependencies(
        sessionmaker, resources.langgraph, deps
    )
    return TaskWorkspaceService(
        sessionmaker,
        research_execution=research_execution,
        artifact_service=artifact_service,
    )


def get_workflow_service(request: Request) -> WorkflowService:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return WorkflowService(resources.database.session_factory())


def get_langgraph_checkpoint_manager(request: Request) -> LangGraphCheckpointManager:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.langgraph is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.langgraph


def get_company_identity_service(request: Request) -> CompanyIdentityService:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return CompanyIdentityService(resources.database.session_factory())


def get_source_registry_service(request: Request) -> SourceRegistryService:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return SourceRegistryService(resources.database.session_factory())


def get_raw_storage(request: Request) -> LocalRawArtifactStore:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.raw_storage is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.raw_storage


def get_export_storage(request: Request) -> ExportArtifactStore:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.export_storage is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.export_storage


def get_report_export_service(request: Request) -> ReportExportService:
    """确定性导出服务（Stage 6C spec H/M/N）。

    复用 `create_stage5_dependencies` 装配的同一批 Services（verify 链共享），
    经 `TaskArtifactService.from_dependencies` 恢复 canonical lineage——导出路径
    **0 LLM / 0 Retrieval / 0 Chroma / 0 Web**，不依赖 DEEPSEEK_API_KEY（构造
    deps 不调 API）。字节归档走 `resources.export_storage`（内容寻址）。
    """
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None or resources.langgraph is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    settings = request.app.state.settings
    sessionmaker = resources.database.session_factory()
    deps = create_stage5_dependencies(settings, sessionmaker)
    artifact_service = TaskArtifactService.from_dependencies(
        sessionmaker, resources.langgraph, deps
    )
    return ReportExportService(
        sessionmaker,
        artifact_service,
        report_service=deps.report_service,
        report_check_service=deps.report_check_service,
        report_audit_service=deps.report_audit_service,
        review_action_service=deps.review_action_service,
        company_service=CompanyIdentityService(sessionmaker),
        export_store=resources.export_storage,
    )


def get_source_ingestion_service(request: Request) -> SourceIngestionService:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    settings = request.app.state.settings
    return SourceIngestionService(
        sessionmaker=resources.database.session_factory(),
        raw_store=resources.raw_storage,
        max_bytes=settings.source_max_file_size_bytes,
    )


def get_source_preparation_service(request: Request) -> SourcePreparationService | None:
    """V1.1 P0-2：共享 SourcePreparationService（lifespan 装配；后台任务生命周期
    由服务实例管理，不随请求结束）。资源未绑定（测试 app / 异常配置）→ None，
    调用方跳过后台预准备（上传/导入主路径不受影响）。"""
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.source_preparation is None:
        return None
    return resources.source_preparation
