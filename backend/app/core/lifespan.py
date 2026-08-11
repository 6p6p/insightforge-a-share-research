"""Application lifespan: create and tear down shared resources."""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import get_logger
from app.core.resources import ApplicationResources
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.services.company_identity_service import CompanyIdentityService
from app.services.research_execution_recovery import ResearchExecutionRecoveryCoordinator
from app.services.research_execution_service import ResearchExecutionService
from app.services.workflow_recovery_service import WorkflowRecoveryService
from app.storage.raw_store import LocalRawArtifactStore
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager
from app.workflows.runner import WorkflowRunner

logger = get_logger("app.lifespan")


def _create_research_execution(
    settings,
    sessionmaker,
    langgraph: LangGraphCheckpointManager,
) -> ResearchExecutionService:
    """惰性 runner factory：只在真正启动研究时才构建 Stage4/Stage5 deps。

    生产 factory 从 Settings 构建真实 model（构造不调 API）；自动测试通过
    dependency_overrides 注入 Fake deps，因此启动路径不会触碰真实 LLM。
    """
    from app.stage4.dependencies import create_stage4_dependencies
    from app.stage4.runner import Stage4WorkflowRunner
    from app.stage5.dependencies import create_stage5_dependencies
    from app.stage5.runner import Stage5WorkflowRunner

    def _stage4_factory() -> Stage4WorkflowRunner:
        deps = create_stage4_dependencies(settings, sessionmaker)
        return Stage4WorkflowRunner(sessionmaker, langgraph, deps)

    def _stage5_factory() -> Stage5WorkflowRunner:
        deps = create_stage5_dependencies(settings, sessionmaker)
        return Stage5WorkflowRunner(sessionmaker, langgraph, deps)

    return ResearchExecutionService(
        sessionmaker=sessionmaker,
        checkpoint_manager=langgraph,
        company_identity=CompanyIdentityService(sessionmaker),
        stage4_runner_factory=_stage4_factory,
        stage5_runner_factory=_stage5_factory,
        shutdown_timeout_seconds=settings.workflow_shutdown_timeout_seconds,
    )


@asynccontextmanager
async def lifespan(application: FastAPI) -> AsyncIterator[None]:
    settings = application.state.settings
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=settings.database_echo,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    chroma = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    langgraph = LangGraphCheckpointManager(
        connection_uri=to_postgres_connection_uri(settings.database_url)
    )
    sessionmaker = database.session_factory()
    workflow_execution = WorkflowExecutionManager(
        runner=WorkflowRunner(sessionmaker, langgraph),
        shutdown_timeout_seconds=settings.workflow_shutdown_timeout_seconds,
        sessionmaker=sessionmaker,
    )
    research_execution = _create_research_execution(settings, sessionmaker, langgraph)
    raw_storage = LocalRawArtifactStore(
        root=settings.raw_storage_root,
        max_bytes=settings.source_max_file_size_bytes,
        max_json_bytes=settings.macro_max_json_response_bytes,
    )
    resources = ApplicationResources(
        database=database,
        chroma=chroma,
        langgraph=langgraph,
        workflow_execution=workflow_execution,
        research_execution=research_execution,
        raw_storage=raw_storage,
    )
    application.state.resources = resources
    logger.info("application_startup", environment=settings.app_env)
    # best-effort reconcile：在接受新 WorkflowRun 前完成；PostgreSQL 不可用不阻止启动
    try:
        recovery = WorkflowRecoveryService(sessionmaker)
        await asyncio.wait_for(
            recovery.reconcile_orphaned_runs(),
            timeout=settings.workflow_reconcile_timeout_seconds,
        )
    except Exception as exc:
        logger.warning(
            "orphaned_runs_reconcile_failed",
            error_type=type(exc).__name__,
        )
    # best-effort 恢复研究链（spec E）：reconcile 之后、接受新执行之前，把
    # 「Stage4 已完成但 Stage5 未创建」的 task 重新调度 Stage5 续接；失败不阻止启动。
    try:
        coordinator = ResearchExecutionRecoveryCoordinator(sessionmaker, research_execution)
        await asyncio.wait_for(
            coordinator.recover_interrupted_chains(),
            timeout=settings.workflow_reconcile_timeout_seconds,
        )
    except Exception as exc:
        logger.warning(
            "research_chains_recover_failed",
            error_type=type(exc).__name__,
        )
    try:
        yield
    finally:
        try:
            await resources.workflow_execution.close()
        except Exception as exc:
            logger.warning(
                "workflow_execution_close_failed",
                error_type=type(exc).__name__,
            )
        try:
            await resources.research_execution.close()
        except Exception as exc:
            logger.warning(
                "research_execution_close_failed",
                error_type=type(exc).__name__,
            )
        try:
            await resources.langgraph.close()
        except Exception as exc:
            logger.warning("checkpoint_close_failed", error_type=type(exc).__name__)
        await resources.database.dispose()
        application.state.resources = None
        logger.info("application_shutdown")
