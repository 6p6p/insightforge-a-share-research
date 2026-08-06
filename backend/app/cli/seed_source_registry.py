"""CLI: seed the source registry with default providers."""

import asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.services.source_registry_service import SourceRegistryService

configure_asyncio_runtime()


async def _main() -> None:
    settings = get_settings()
    database = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    try:
        service = SourceRegistryService(database.session_factory())
        result = await service.seed_defaults()
        print("source registry seeded")
        print(f"inserted_or_updated: {result.inserted_or_updated}")
        print(f"total: {result.total}")
    finally:
        await database.dispose()


if __name__ == "__main__":
    asyncio.run(_main())
