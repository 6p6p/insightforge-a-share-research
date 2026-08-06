"""FastAPI dependency accessors for the ChromaManager."""

from fastapi import Request

from app.vectorstore.client import ChromaManager


def get_chroma(request: Request) -> ChromaManager:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.chroma is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.chroma
