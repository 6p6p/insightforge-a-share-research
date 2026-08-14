"""Evaluation benchmark summary endpoint (stage 7B.1.4E / §25 web view).

**只读诊断视图**：把 CLI 产物（`benchmark/run_<mode>/results.json`）原样暴露给
前端比较页。不提供任何写操作、不执行 attempt、不读取 eval 持久化表。

- workspace 根目录解析：`EVAL_BENCHMARK_WORKSPACE`（环境变量）→ 否则 repo 根
  `benchmark/`；
- `GET /eval/benchmark/summary?run=fake|real`：缺失/非法 → 404 明确消息；
- 响应体即 results.json 的 payload（schema 由 CLI 保证，不做二次校验）。

安全：不打印 API key / prompt / output 文本；错误消息只含路径名与状态。
"""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException

router = APIRouter(tags=["eval"])

_RUN_MODES = ("fake", "real")


def _workspace_root() -> Path:
    configured = os.environ.get("EVAL_BENCHMARK_WORKSPACE")
    if configured:
        return Path(configured)
    # 默认：repo 根 / benchmark（backend/app/api/v1/routes/eval_benchmark.py → parents[4]）。
    return Path(__file__).resolve().parents[4] / "benchmark"


@router.get("/eval/benchmark/summary")
async def eval_benchmark_summary(run: str = "fake") -> dict:
    if run not in _RUN_MODES:
        raise HTTPException(status_code=404, detail=f"未知 benchmark run 模式: {run}")
    results_path = _workspace_root() / f"run_{run}" / "results.json"
    if not results_path.is_file():
        raise HTTPException(
            status_code=404,
            detail=(
                f"benchmark run 结果不存在: {results_path.name}（先执行 "
                f"`python -m app.eval.cli run --{run} ...` 生成）"
            ),
        )
    import json

    try:
        payload = json.loads(results_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=500, detail="benchmark 结果解析失败") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("attempts"), list):
        raise HTTPException(status_code=500, detail="benchmark 结果 schema 非法")
    return payload
