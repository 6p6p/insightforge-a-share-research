"""Shared application resources created during lifespan."""

from app.db.session import DatabaseManager
from app.research_orchestration.service import ResearchOrchestrationService
from app.services.research_execution_service import ResearchExecutionService
from app.services.source_preparation_service import SourcePreparationService
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
        research_orchestration: ResearchOrchestrationService,
        raw_storage: LocalRawArtifactStore,
        export_storage: ExportArtifactStore,
        source_preparation: SourcePreparationService | None = None,
    ) -> None:
        self.database = database
        self.chroma = chroma
        self.langgraph = langgraph
        self.workflow_execution = workflow_execution
        self.research_execution = research_execution
        self.research_orchestration = research_orchestration
        self.raw_storage = raw_storage
        self.export_storage = export_storage
        self.source_preparation = source_preparation
