"""Tests for the shared asyncio runtime configuration."""

import asyncio
import sys

from app.core.runtime import configure_asyncio_runtime


def test_runtime_is_reentrant() -> None:
    configure_asyncio_runtime()
    configure_asyncio_runtime()


def test_win32_sets_selector_policy(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    calls = []

    class FakePolicy:
        pass

    monkeypatch.setattr(asyncio, "WindowsSelectorEventLoopPolicy", lambda: FakePolicy())
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: calls.append(policy))

    configure_asyncio_runtime()

    assert len(calls) == 1
    assert isinstance(calls[0], FakePolicy)


def test_non_windows_does_not_set_policy(monkeypatch) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    calls = []
    monkeypatch.setattr(asyncio, "set_event_loop_policy", lambda policy: calls.append(policy))

    configure_asyncio_runtime()

    assert calls == []
