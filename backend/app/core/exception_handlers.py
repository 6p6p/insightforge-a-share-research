"""Exception handlers producing the unified error envelope."""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.core.errors import DomainError
from app.schemas.error import ErrorDetail, ErrorEnvelope


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=exc.code,
            message=exc.message,
            request_id=_request_id(request),
        )
    )
    return JSONResponse(status_code=exc.http_status, content=envelope.model_dump())


def register_exception_handlers(application: FastAPI) -> None:
    application.add_exception_handler(DomainError, domain_error_handler)
