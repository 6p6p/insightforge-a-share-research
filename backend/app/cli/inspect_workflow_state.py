"""CLI: read the final LangGraph state for a thread from a fresh process."""

import argparse
import asyncio
import json
import sys

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.urls import to_postgres_connection_uri
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.graph import build_research_workflow

configure_asyncio_runtime()


async def _main(thread_id: str) -> None:
    settings = get_settings()
    manager = LangGraphCheckpointManager(
        connection_uri=to_postgres_connection_uri(settings.database_url)
    )
    try:
        graph = build_research_workflow(await manager.get_checkpointer())
        config = {"configurable": {"thread_id": thread_id}}
        state = await graph.aget_state(config)
    finally:
        await manager.close()

    if state is None or not state.values:
        print(f"no state found for thread {thread_id}", file=sys.stderr)
        raise SystemExit(1)

    values = state.values
    result = {
        "thread_id": thread_id,
        "task_id": values.get("task_id"),
        "run_id": values.get("run_id"),
        "progress": values.get("progress"),
        "completed_nodes": values.get("completed_nodes"),
        "simulation_complete": values.get("simulation_complete"),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Inspect the final LangGraph state for a thread")
    parser.add_argument("--thread-id", required=True, help="thread_id (= run_id) to inspect")
    args = parser.parse_args()
    asyncio.run(_main(args.thread_id))
