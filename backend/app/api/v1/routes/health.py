"""Health check endpoints: liveness and readiness."""

from fastapi import APIRouter, Request

from app.core.config import get_package_version
from app.schemas.health import LiveHealthResponse, ReadyChecks, ReadyHealthResponse

router = APIRouter(tags=["health"])


@router.get("/health/live", response_model=LiveHealthResponse)
async def health_live() -> LiveHealthResponse:
    return LiveHealthResponse()


@router.get("/health/ready", response_model=ReadyHealthResponse)
async def health_ready(request: Request) -> ReadyHealthResponse:
    settings = request.app.state.settings
    return ReadyHealthResponse(
        service="insightforge-backend",
        version=get_package_version(),
        environment=settings.app_env,
        checks=ReadyChecks(),
    )
