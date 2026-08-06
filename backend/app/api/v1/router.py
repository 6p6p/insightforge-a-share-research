"""API v1 router aggregation."""

from fastapi import APIRouter

from app.api.v1.routes import companies, health, source_registry, tasks, workflows

api_router = APIRouter()
api_router.include_router(health.router)
api_router.include_router(tasks.router)
api_router.include_router(workflows.router)
api_router.include_router(companies.router)
api_router.include_router(source_registry.router)
