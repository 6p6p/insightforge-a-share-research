"""Health check response models."""

from typing import Literal

from pydantic import BaseModel


class LiveHealthResponse(BaseModel):
    status: Literal["ok"] = "ok"


class ReadyChecks(BaseModel):
    configuration: Literal["ok"] = "ok"


class ReadyHealthResponse(BaseModel):
    status: Literal["ok"] = "ok"
    service: str
    version: str
    environment: str
    checks: ReadyChecks
