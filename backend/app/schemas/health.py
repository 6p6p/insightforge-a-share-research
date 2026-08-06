"""Health check response models."""

from typing import Literal

from pydantic import BaseModel

CheckStatus = Literal["ok", "error"]


class LiveHealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyChecks(BaseModel):
    configuration: CheckStatus = "ok"
    database: CheckStatus = "ok"
    chroma: CheckStatus = "ok"
    checkpoint: CheckStatus = "ok"


class ReadyHealthResponse(BaseModel):
    status: Literal["ok", "not_ready"] = "ok"
    service: str
    version: str
    environment: str
    checks: ReadyChecks
