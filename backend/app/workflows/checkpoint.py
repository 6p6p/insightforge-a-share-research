"""LangGraph checkpoint manager backed by AsyncPostgresSaver."""

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer


def _strict_serde() -> JsonPlusSerializer:
    """Strict MsgPack: allow only built-in JSON/MsgPack safe types."""
    return JsonPlusSerializer(allowed_msgpack_modules=None)


class LangGraphCheckpointManager:
    """Lazily owns an AsyncPostgresSaver for one PostgreSQL connection URI."""

    def __init__(self, connection_uri: str) -> None:
        self._connection_uri = connection_uri
        self._lock = asyncio.Lock()
        self._context = None
        self._checkpointer = None

    async def _ensure(self) -> AsyncPostgresSaver:
        if self._checkpointer is None:
            async with self._lock:
                if self._checkpointer is None:
                    context = AsyncPostgresSaver.from_conn_string(
                        self._connection_uri,
                        serde=_strict_serde(),
                    )
                    checkpointer = await context.__aenter__()
                    self._context = context
                    self._checkpointer = checkpointer
        return self._checkpointer

    async def get_checkpointer(self) -> AsyncPostgresSaver:
        return await self._ensure()

    async def setup(self) -> None:
        """Create vendor checkpoint tables; safe to call repeatedly."""
        checkpointer = await self._ensure()
        await checkpointer.setup()

    async def close(self) -> None:
        """Exit the from_conn_string async context; idempotent and safe when unused."""
        context = None
        async with self._lock:
            if self._context is not None:
                context = self._context
                self._context = None
                self._checkpointer = None
        if context is not None:
            await context.__aexit__(None, None, None)
