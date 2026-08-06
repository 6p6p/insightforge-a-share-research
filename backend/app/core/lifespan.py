"""Application lifespan: create and tear down shared resources."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.core.logging import get_logger
from app.core.resources import ApplicationResources
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager
from app.workflows.runner import WorkflowRunner

logger = get_logger("app.lifespan")


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
    workflow_execution = WorkflowExecutionManager(
        runner=WorkflowRunner(database.session_factory(), langgraph),
        shutdown_timeout_seconds=settings.workflow_shutdown_timeout_seconds,
    )
    resources = ApplicationResources(
        database=database,
        chroma=chroma,
        langgraph=langgraph,
        workflow_execution=workflow_execution,
    )
    application.state.resources = resources
    logger.info("application_startup", environment=settings.app_env)
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
            await resources.langgraph.close()
        except Exception as exc:
            logger.warning("checkpoint_close_failed", error_type=type(exc).__name__)
        await resources.database.dispose()
        application.state.resources = None
        logger.info("application_shutdown")
