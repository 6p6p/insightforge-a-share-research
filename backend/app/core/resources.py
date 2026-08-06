"""Shared application resources created during lifespan."""

from app.db.session import DatabaseManager
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.execution_manager import WorkflowExecutionManager


class ApplicationResources:
    def __init__(
        self,
        database: DatabaseManager,
        chroma: ChromaManager,
        langgraph: LangGraphCheckpointManager,
        workflow_execution: WorkflowExecutionManager,
    ) -> None:
        self.database = database
        self.chroma = chroma
        self.langgraph = langgraph
        self.workflow_execution = workflow_execution
