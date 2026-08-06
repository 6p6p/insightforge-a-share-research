"""Shared application resources created during lifespan."""

from app.db.session import DatabaseManager
from app.vectorstore.client import ChromaManager
from app.workflows.checkpoint import LangGraphCheckpointManager


class ApplicationResources:
    def __init__(
        self,
        database: DatabaseManager,
        chroma: ChromaManager,
        langgraph: LangGraphCheckpointManager,
    ) -> None:
        self.database = database
        self.chroma = chroma
        self.langgraph = langgraph
