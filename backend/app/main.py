"""FastAPI application factory."""

import asyncio
import sys

from fastapi import FastAPI

from app.api.v1.router import api_router
from app.core.config import Settings, get_package_version, get_settings
from app.core.lifespan import lifespan
from app.core.logging import configure_logging
from app.core.middleware import request_tracing_middleware

# psycopg async requires a selector event loop on Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def create_app(settings: Settings | None = None) -> FastAPI:
    resolved = settings or get_settings()
    configure_logging(resolved.log_level)

    application = FastAPI(
        title=resolved.app_name,
        version=get_package_version(),
        lifespan=lifespan,
    )
    application.state.settings = resolved
    application.middleware("http")(request_tracing_middleware)
    application.include_router(api_router, prefix=resolved.api_v1_prefix)
    return application


app = create_app()
