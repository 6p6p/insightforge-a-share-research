"""Tests for the LangGraph checkpoint manager using fakes."""

import asyncio

import pytest

from app.workflows.checkpoint import LangGraphCheckpointManager


class _FakeSaver:
    def __init__(self) -> None:
        self.setup_calls = 0

    async def setup(self) -> None:
        self.setup_calls += 1


class _FakeContext:
    def __init__(self) -> None:
        self.saver = _FakeSaver()
        self.exited = False

    async def __aenter__(self) -> _FakeSaver:
        return self.saver

    async def __aexit__(self, *args) -> bool:
        self.exited = True
        return False


def _manager_with_fake(monkeypatch, contexts: list):
    def fake_from(conn_string: str, *, serde=None):
        context = _FakeContext()
        contexts.append(context)
        return context

    monkeypatch.setattr(
        "app.workflows.checkpoint.AsyncPostgresSaver.from_conn_string",
        fake_from,
    )
    return LangGraphCheckpointManager("postgresql://u:p@host/db")


@pytest.mark.asyncio
async def test_lazy_initialization(monkeypatch) -> None:
    contexts: list = []
    manager = _manager_with_fake(monkeypatch, contexts)

    assert manager._checkpointer is None
    checkpointer = await manager.get_checkpointer()
    assert checkpointer is not None
    assert len(contexts) == 1
    assert manager._checkpointer is not None


@pytest.mark.asyncio
async def test_concurrent_initialization_once(monkeypatch) -> None:
    contexts: list = []
    manager = _manager_with_fake(monkeypatch, contexts)

    first, second = await asyncio.gather(
        manager.get_checkpointer(),
        manager.get_checkpointer(),
    )
    assert len(contexts) == 1
    assert first is second


@pytest.mark.asyncio
async def test_setup_calls_official_setup_and_is_reentrant(monkeypatch) -> None:
    contexts: list = []
    manager = _manager_with_fake(monkeypatch, contexts)

    await manager.setup()
    await manager.setup()
    assert contexts[0].saver.setup_calls == 2


@pytest.mark.asyncio
async def test_close_exits_context_and_is_idempotent(monkeypatch) -> None:
    contexts: list = []
    manager = _manager_with_fake(monkeypatch, contexts)

    await manager.get_checkpointer()
    await manager.close()
    assert contexts[0].exited is True
    assert manager._checkpointer is None

    await manager.close()  # 幂等
    assert manager._checkpointer is None


@pytest.mark.asyncio
async def test_close_without_initialization_is_safe() -> None:
    manager = LangGraphCheckpointManager("postgresql://u:p@host/db")
    await manager.close()
