"""FastAPI dependency wiring for task and workflow services."""

from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dependencies import get_db_session
from app.repositories.research_task_repository import ResearchTaskRepository
from app.services.company_identity_service import CompanyIdentityService
from app.services.source_registry_service import SourceRegistryService
from app.services.task_service import TaskService
from app.services.workflow_service import WorkflowService
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager


def get_task_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TaskService:
    repository = ResearchTaskRepository(session)
    return TaskService(repository)


def get_workflow_execution_manager(request: Request) -> WorkflowExecutionManager:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.workflow_execution is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.workflow_execution


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
