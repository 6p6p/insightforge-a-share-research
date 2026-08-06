"""CLI: create and execute a simulation workflow run for an existing research task."""

import argparse
import asyncio
from uuid import UUID

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.db.urls import to_postgres_connection_uri
from app.workflows.checkpoint import LangGraphCheckpointManager
from app.workflows.runner import WorkflowRunner

configure_asyncio_runtime()


async def _main(task_id: str) -> None:
    settings = get_settings()
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    checkpoint = LangGraphCheckpointManager(
        connection_uri=to_postgres_connection_uri(settings.database_url)
    )
    runner = WorkflowRunner(database.session_factory(), checkpoint)
    try:
        run, result = await runner.create_and_execute(UUID(task_id))
        print(f"run_id: {run.run_id}")
        print(f"thread_id: {run.thread_id}")
        print(f"status: {run.status}")
        print(f"completed_nodes: {result.get('completed_nodes')}")
        print(f"simulation_complete: {result.get('simulation_complete')}")
    finally:
        await checkpoint.close()
        await database.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run a simulation workflow for a task")
    parser.add_argument("--task-id", required=True, help="ResearchTask task_id (UUID)")
    args = parser.parse_args()
    asyncio.run(_main(args.task_id))
