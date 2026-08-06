"""FastAPI dependency accessors for the DatabaseManager."""

from fastapi import Request

from app.db.session import DatabaseManager


def get_database(request: Request) -> DatabaseManager:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.database
