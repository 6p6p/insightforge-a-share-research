"""Chroma client lifecycle management."""

import asyncio

import chromadb
from chromadb.api.async_api import AsyncClientAPI


class ChromaManager:
    """Lazily initialised async Chroma HTTP client, safe for concurrent access.

    Note: chromadb-client 1.5.9's async client (AsyncClientAPI / AsyncClient)
    exposes no public async close()/aclose() interface. We therefore do not
    call private internals and rely on process shutdown for resource release.
    """

    def __init__(
        self,
        host: str,
        port: int,
        ssl: bool = False,
        timeout_seconds: int = 5,
    ) -> None:
        self._host = host
        self._port = port
        self._ssl = ssl
        self._timeout_seconds = timeout_seconds
        self._client: AsyncClientAPI | None = None
        self._lock = asyncio.Lock()

    async def _get_client(self) -> AsyncClientAPI:
        if self._client is None:
            async with self._lock:
                if self._client is None:
                    self._client = await chromadb.AsyncHttpClient(
                        host=self._host,
                        port=self._port,
                        ssl=self._ssl,
                        settings=chromadb.config.Settings(anonymized_telemetry=False),
                    )
        return self._client

    async def get_client(self) -> AsyncClientAPI:
        """Return the shared async Chroma client (lazily initialised, thread-safe)."""
        return await self._get_client()

    async def heartbeat(self) -> None:
        """Verify service connectivity only; never creates collections."""
        client = await self._get_client()
        await asyncio.wait_for(client.heartbeat(), timeout=self._timeout_seconds)
