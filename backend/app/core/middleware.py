"""Per-request tracing middleware: request_id propagation, timing and structured logs."""

import time
import uuid

from fastapi import Request
from structlog.contextvars import bind_contextvars, clear_contextvars

from app.core.logging import get_logger

logger = get_logger("app.middleware")


async def request_tracing_middleware(request: Request, call_next):
    request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())

    clear_contextvars()
    bind_contextvars(request_id=request_id)

    start = time.perf_counter()
    try:
        logger.info("request_start", method=request.method, path=request.url.path)
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000.0

        response.headers["X-Request-ID"] = request_id
        logger.info(
            "request_end",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(duration_ms, 3),
        )
        return response
    except Exception:
        logger.exception(
            "request_failed",
            method=request.method,
            path=request.url.path,
        )
        raise
    finally:
        clear_contextvars()
