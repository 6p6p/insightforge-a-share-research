"""Shared asyncio runtime configuration."""

import asyncio
import sys


def configure_asyncio_runtime() -> None:
    """Configure the asyncio runtime for cross-platform compatibility.

    Psycopg async mode requires a SelectorEventLoop on Windows; the default
    ProactorEventLoop is incompatible. Linux/macOS are left untouched.
    Safe to call repeatedly.
    """
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
