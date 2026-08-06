"""CLI: create the LangGraph checkpoint tables owned by langgraph-checkpoint-postgres."""

import asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.urls import to_postgres_connection_uri
from app.workflows.checkpoint import LangGraphCheckpointManager

configure_asyncio_runtime()


async def _main() -> None:
    settings = get_settings()
    manager = LangGraphCheckpointManager(
        connection_uri=to_postgres_connection_uri(settings.database_url)
    )
    try:
        await manager.setup()
    finally:
        await manager.close()
    print("checkpointer setup complete")


if __name__ == "__main__":
    asyncio.run(_main())
