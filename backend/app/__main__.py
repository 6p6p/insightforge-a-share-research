"""Backend server entry point (`python -m app`).

跨平台启动入口：在 uvicorn 创建事件循环**之前**配置 asyncio runtime，并显式
指定 loop factory（Windows 默认 ProactorEventLoop 与 psycopg async 不兼容，需
SelectorEventLoop；`python -m uvicorn` CLI 与 `uvicorn.run()` 的 loop 创建路径
在部分版本绕过 event loop policy，因此必须经本入口 + 显式 loop_factory 启动）。
"""

from __future__ import annotations

import asyncio
import sys

import uvicorn

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime


def _loop_factory() -> asyncio.AbstractEventLoop:
    if sys.platform == "win32":
        return asyncio.SelectorEventLoop()
    return asyncio.new_event_loop()


def main() -> None:
    configure_asyncio_runtime()
    settings = get_settings()
    config = uvicorn.Config(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
    )
    server = uvicorn.Server(config)
    asyncio.run(server.serve(), loop_factory=_loop_factory)


if __name__ == "__main__":
    main()
