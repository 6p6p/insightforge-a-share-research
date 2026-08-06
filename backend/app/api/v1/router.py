"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1.routes import health, tasks, workflows

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(tasks.router)
api_router.include_router(workflows.router)
