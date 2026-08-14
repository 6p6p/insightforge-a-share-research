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

    # Linux 上 asyncio 无 WindowsSelectorEventLoopPolicy 属性：raising=False
    # 让 monkeypatch 在任意平台创建 mock 属性（Windows 上则覆盖真实类）。
    monkeypatch.setattr(
        asyncio,
        "WindowsSelectorEventLoopPolicy",
        lambda: FakePolicy(),
        raising=False,
    )
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
