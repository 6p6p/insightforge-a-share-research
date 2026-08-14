"""run_backend launcher runtime test（Gate 0 / 4B.1 closeout）。

验证 launcher 在调用 `uvicorn.run` **之前**已配置 asyncio runtime，且显式传
`loop="none"`——Windows host 下 psycopg async `/ready`（database / checkpoint）
能过的前提。

- import launcher 即执行 `configure_asyncio_runtime()`（模块级，在任何
  uvicorn.run / asyncio.run 之前）；
- `main()` 内同样先 configure 再 uvicorn.run（显式顺序，与模块级幂等叠加）；
- `uvicorn.run(..., loop="none")`：uvicorn 0.52 的 asyncio_loop_factory 在
  Windows 上硬编码 ProactorEventLoop（loop="auto" 默认），会绕过 policy；
  只有 loop="none" 才让 uvicorn 退回 asyncio.run() 用当前 policy 建 loop。

零 DB / 零网络 / 零真实 uvicorn（uvicorn.run 被 monkeypatch）。
"""

import asyncio
import sys

from app.cli import run_backend
from app.core.config import Settings


def _test_settings() -> Settings:
    """clean environment（CI 无本地 .env）：显式 test Settings（fake loopback DB）。"""
    return Settings(
        _env_file=None,
        app_env="test",
        app_host="127.0.0.1",
        app_port=8001,
        log_level="INFO",
        database_url="postgresql+psycopg://user:pass@127.0.0.1:5433/insightforge",
    )


def test_launcher_import_configures_asyncio_runtime() -> None:
    """import 模块即完成 runtime 配置（Windows → SelectorEventLoop）。"""
    if sys.platform == "win32":
        assert isinstance(asyncio.get_event_loop_policy(), asyncio.WindowsSelectorEventLoopPolicy)


def test_main_calls_configure_before_uvicorn_run(monkeypatch) -> None:
    """main() 内 configure_asyncio_runtime 必须排在 uvicorn.run 之前。"""
    order: list[str] = []
    monkeypatch.setattr(run_backend, "configure_asyncio_runtime", lambda: order.append("configure"))
    monkeypatch.setattr(run_backend, "get_settings", _test_settings)
    captured: dict = {}

    def fake_run(*args, **kwargs):
        order.append("run")
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(run_backend.uvicorn, "run", fake_run)
    run_backend.main()

    assert order == ["configure", "run"]
    assert captured["args"][0] == "app.main:app"
    assert isinstance(captured["kwargs"]["host"], str) and captured["kwargs"]["host"]
    assert isinstance(captured["kwargs"]["port"], int)
    assert isinstance(captured["kwargs"]["log_level"], str)
    assert captured["kwargs"]["loop"] == "none"
