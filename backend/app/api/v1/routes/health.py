"""Health check endpoints: liveness and readiness."""

import asyncio
from collections.abc import Awaitable
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response

from app.core.config import get_package_version
from app.core.logging import get_logger
from app.db.dependencies import get_database
from app.db.session import DatabaseManager
from app.schemas.health import (
    CheckStatus,
    LiveHealthResponse,
    ReadyChecks,
    ReadyHealthResponse,
)
from app.vectorstore.client import ChromaManager
from app.vectorstore.dependencies import get_chroma

router = APIRouter(tags=["health"])
logger = get_logger("app.health")


@router.get("/health/live", response_model=LiveHealthResponse)
async def health_live() -> LiveHealthResponse:
    return LiveHealthResponse()


async def _configuration_ok() -> None:
    return None


async def _probe(name: str, pending: Awaitable[None]) -> tuple[str, CheckStatus]:
    try:
        await pending
    except Exception as exc:
        # 脱敏：只记录异常类型，不记录可能含连接信息的完整异常
        logger.warning("health_check_failed", check=name, error_type=type(exc).__name__)
        return name, "error"
    return name, "ok"


@router.get(
    "/health/ready",
    response_model=ReadyHealthResponse,
    responses={503: {"model": ReadyHealthResponse}},
)
async def health_ready(
    request: Request,
    response: Response,
    database: Annotated[DatabaseManager, Depends(get_database)],
    chroma: Annotated[ChromaManager, Depends(get_chroma)],
) -> ReadyHealthResponse:
    settings = request.app.state.settings

    results = dict(
        await asyncio.gather(
            _probe("configuration", _configuration_ok()),
            _probe("database", database.ping()),
            _probe("chroma", chroma.heartbeat()),
        )
    )

    checks = ReadyChecks(**results)
    if all(status == "ok" for status in results.values()):
        status = "ok"
    else:
        status = "not_ready"
        response.status_code = 503

    return ReadyHealthResponse(
        status=status,
        service="insightforge-backend",
        version=get_package_version(),
        environment=settings.app_env,
        checks=checks,
    )
