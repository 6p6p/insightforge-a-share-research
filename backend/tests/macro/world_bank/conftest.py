"""World Bank provider test fixtures (MockTransport only, no real DB)."""

from __future__ import annotations

import httpx
import pytest

from app.macro.world_bank.client import REQUEST_LIMIT, WorldBankClient
from app.macro.world_bank.provider import WorldBankProvider
from tests.macro.world_bank.helpers import FakeSessionFactory, make_provider_row

_DEFAULT = object()


@pytest.fixture
def world_bank_provider(monkeypatch):
    """构造 WorldBankProvider：注入 fake session factory，并可选地把 MockTransport 接入客户端。"""

    def _build(*, row: object = _DEFAULT, transport: httpx.AsyncBaseTransport | None = None):
        if row is _DEFAULT:
            row = make_provider_row()
        if transport is not None:
            real_init = WorldBankClient.__init__

            def _patched_init(
                self,
                *,
                allowed_domains: list[str],
                timeout: httpx.Timeout | None = None,
                request_limit: int = REQUEST_LIMIT,
            ) -> None:
                real_init(
                    self,
                    allowed_domains=allowed_domains,
                    transport=transport,
                    timeout=timeout,
                    request_limit=request_limit,
                )

            monkeypatch.setattr(WorldBankClient, "__init__", _patched_init)
        factory = FakeSessionFactory(row)
        return WorldBankProvider(factory), factory

    return _build
