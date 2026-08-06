"""Integration tests requiring live PostgreSQL and Chroma services.

Run explicitly with:
    conda run -n insightforge python -m pytest \
        -c backend/pyproject.toml backend/tests/integration -m integration -v
"""

import asyncio
import sys

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.db.session import DatabaseManager
from app.vectorstore.client import ChromaManager

pytestmark = pytest.mark.integration

# psycopg async requires a selector event loop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def chroma() -> ChromaManager:
    settings = get_settings()
    manager = ChromaManager(
        host=settings.chroma_host,
        port=settings.chroma_port,
        ssl=settings.chroma_ssl,
        timeout_seconds=settings.chroma_timeout_seconds,
    )
    yield manager


@pytest.mark.asyncio
async def test_postgres_select_1(database: DatabaseManager) -> None:
    await database.ping()


@pytest.mark.asyncio
async def test_chroma_heartbeat(chroma: ChromaManager) -> None:
    await chroma.heartbeat()
