"""FastAPI dependency accessors for the DatabaseManager and its sessions."""

from collections.abc import AsyncIterator

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import DatabaseManager


def get_database(request: Request) -> DatabaseManager:
    resources = getattr(request.app.state, "resources", None)
    if resources is None or resources.database is None:
        raise RuntimeError("application resources are not initialized; lifespan must create them")
    return resources.database


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    database = get_database(request)
    session_factory = database.session_factory()
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
