"""Shared application resources created during lifespan."""

from app.db.session import DatabaseManager
from app.services.research_execution_service import ResearchExecutionService
from app.storage.export_store import ExportArtifactStore
from app.storage.raw_store import LocalRawArtifactStore
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
        research_execution: ResearchExecutionService,
        raw_storage: LocalRawArtifactStore,
        export_storage: ExportArtifactStore,
    ) -> None:
        self.database = database
        self.chroma = chroma
        self.langgraph = langgraph
        self.workflow_execution = workflow_execution
        self.research_execution = research_execution
        self.raw_storage = raw_storage
        self.export_storage = export_storage
