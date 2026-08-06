"""Shared application resources created during lifespan."""

from app.db.session import DatabaseManager
from app.vectorstore.client import ChromaManager


class ApplicationResources:
    def __init__(self, database: DatabaseManager, chroma: ChromaManager) -> None:
        self.database = database
        self.chroma = chroma
