"""FastAPI dependency wiring for task services."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.dependencies import get_db_session
from app.repositories.research_task_repository import ResearchTaskRepository
from app.services.task_service import TaskService


def get_task_service(
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> TaskService:
    repository = ResearchTaskRepository(session)
    return TaskService(repository)
