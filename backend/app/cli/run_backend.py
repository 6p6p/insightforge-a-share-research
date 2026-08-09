"""Windows host backend launcher（官方开发入口）。

`python -m uvicorn app.main:app` 在 Windows 上会**先**用默认 ProactorEventLoop
policy 创建 event loop，**之后**才 import `app.main`；等 `app.main` 模块级调用
`configure_asyncio_runtime()` 时 loop 已经创建，policy 变更不再生效，
psycopg async 的 `/api/v1/health/ready`（database / checkpoint）会失败。

本 launcher 需要**两个条件同时成立**才能在 Windows host 上用 SelectorEventLoop：

1. 在任何 `uvicorn.run` / `asyncio.run` **之前**显式调用
   `configure_asyncio_runtime()`（把全局 policy 设为 WindowsSelectorEventLoopPolicy）；
2. 给 `uvicorn.run(..., loop="none")`——uvicorn 0.52 的 `asyncio_loop_factory`
   在 Windows 上**直接 new `asyncio.ProactorEventLoop`**（`loop="auto"` 默认值），
   **绕过事件循环 policy**；只有 `loop="none"` 才让 uvicorn 退回到
   `asyncio.run()` 语义，用当前 policy 创建 loop，`configure_asyncio_runtime()`
   才真正生效。

实测（2026-08-09，Windows 11 host + uvicorn 0.52.1 + PostgreSQL 18.4）：
只有 `configure_asyncio_runtime()` 时 `ready` 的 database / checkpoint 为
`error`（psycopg: "Psycopg cannot use the 'ProactorEventLoop' to run in async
mode"）；加上 `loop="none"` 后 `ready` 五项全部 `ok`。

Docker 入口保持现状（`compose.yaml` 直接 `python -m uvicorn ...`，Linux 容器
默认 SelectorEventLoop，无此问题）。host / port 从 Settings（`app_host` /
`app_port`）读取，不重复造配置。

运行（insightforge Conda 环境，从 backend/ 目录）：
    python -m app.cli.run_backend
"""

import uvicorn

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime

# 必须在 uvicorn.run 之前设置 loop policy：uvicorn 内部 `asyncio.run` 会用当前
# 已生效的 policy 创建 event loop；若等到 app.main import 才设置，
# ProactorEventLoop 已被创建，Windows 上 psycopg async 会失败。
# loop="none" 同样必要：uvicorn 0.52 的 asyncio_loop_factory 在 Windows 上硬编码
# ProactorEventLoop，会绕过这里的 policy（见模块 docstring 实测）。
configure_asyncio_runtime()


def main() -> None:
    # 幂等；无论从哪个路径调用 main()，都保证在任何 uvicorn.run 之前已配置
    # asyncio runtime（模块级 import 已配置一次，这里再兜底一次）。
    configure_asyncio_runtime()
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.app_host,
        port=settings.app_port,
        log_level=settings.log_level.lower(),
        # loop="none"：让 uvicorn 退回到 asyncio.run()，用已配置的
        # WindowsSelectorEventLoopPolicy 创建 SelectorEventLoop（见模块 docstring）。
        loop="none",
    )


if __name__ == "__main__":
    main()
