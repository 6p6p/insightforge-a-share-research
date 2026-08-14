"""Evaluation CLI（stage 7B.1.4E）：dataset / run / score / report。

用法（backend root 下，insightforge conda env）：
    python -m app.eval.cli dataset --root <dir>
    python -m app.eval.cli run --dataset <dir> --workdir <dir> [--real] [--cases ...] [--attempts N]
    python -m app.eval.cli score --workdir <dir>
    python -m app.eval.cli report --workdir <dir>

产物（workdir）：
- `results.json`：全部 attempt 记录（machine-readable，完整性由 fingerprint 保证）；
- `summary.md` / `summary.csv`：人类可读摘要。

安全边界：不打印 API key / prompt / output 文本；错误只报稳定 error code。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.eval.benchmark.dataset import build_benchmark_dataset
from app.eval.benchmark.experiment import (
    BenchmarkExperimentOptions,
    run_benchmark_experiment,
)
from app.eval.variants import EvalVariantId

_DEFAULT_CASES = ("moutai-business", "moutai-financial", "moutai-full")
_VARIANTS_BY_NAME = {variant.value: variant for variant in EvalVariantId}


def _cmd_dataset(args: argparse.Namespace) -> int:
    result = asyncio.run(build_benchmark_dataset(args.root))
    print(f"[dataset] built: {result['dataset_id']} v{result['dataset_version']}")
    for case_id in result["cases"]:
        print(f"[dataset]   case: {case_id}")
    print(f"[dataset] root: {result['root']}")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    cases = tuple(args.cases) if args.cases else _DEFAULT_CASES
    variants = tuple(_VARIANTS_BY_NAME[name] for name in args.variants)
    options = BenchmarkExperimentOptions(
        dataset_root=Path(args.dataset),
        workdir=Path(args.workdir),
        mode="real" if args.real else "fake",
        cases=cases,
        variants=variants,
        attempts=args.attempts,
    )
    payload = asyncio.run(run_benchmark_experiment(options))
    print(
        f"[run] done: {len(payload['attempts'])} attempts -> {Path(args.workdir) / 'results.json'}"
    )
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    """results.json 的评分校验（幂等重渲染；不重跑 attempt）。"""
    results_path = Path(args.workdir) / "results.json"
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    attempts = payload["attempts"]
    failures = [
        record
        for record in attempts
        if record["status"] == "failed" and not record.get("expected_fail_fast", False)
    ]
    for record in failures:
        print(
            f"[score] unexpected failure: {record['case_id']} / "
            f"{record['variant_id']}: {record['error_code']}"
        )
    print(
        f"[score] {len(attempts)} attempts, {len(failures)} unexpected failures, "
        f"{sum(1 for r in attempts if r['persisted'])} scoring-persisted"
    )
    return 1 if failures else 0


def _cmd_report(args: argparse.Namespace) -> int:
    from app.eval.benchmark.experiment import _write_outputs

    results_path = Path(args.workdir) / "results.json"
    payload = json.loads(results_path.read_text(encoding="utf-8"))
    _write_outputs(Path(args.workdir), payload)
    print(f"[report] wrote summary.md / summary.csv into {args.workdir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="insightforge-eval",
        description="InsightForge 三路 variant 可复现 benchmark CLI（stage 7B.1.4E）",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_dataset = sub.add_parser("dataset", help="构建 frozen benchmark dataset")
    p_dataset.add_argument("--root", required=True, help="dataset 输出目录")
    p_dataset.set_defaults(func=_cmd_dataset)

    p_run = sub.add_parser("run", help="运行三路 variant 实验")
    p_run.add_argument("--dataset", required=True, help="dataset 目录（dataset 命令产物）")
    p_run.add_argument("--workdir", required=True, help="结果输出目录")
    p_run.add_argument("--real", action="store_true", help="真实 DeepSeek（默认 fake 离线）")
    p_run.add_argument(
        "--cases",
        nargs="+",
        default=None,
        choices=list(_DEFAULT_CASES),
        help="case 子集（默认全部）",
    )
    p_run.add_argument(
        "--variants",
        nargs="+",
        default=[v.value for v in EvalVariantId],
        choices=sorted(_VARIANTS_BY_NAME),
        help="variant 子集（默认三路）",
    )
    p_run.add_argument("--attempts", type=int, default=1, help="每 (case, variant) attempt 数")
    p_run.set_defaults(func=_cmd_run)

    p_score = sub.add_parser("score", help="校验 results.json（评分一致性）")
    p_score.add_argument("--workdir", required=True, help="含 results.json 的目录")
    p_score.set_defaults(func=_cmd_score)

    p_report = sub.add_parser("report", help="从 results.json 重渲染 summary.md / summary.csv")
    p_report.add_argument("--workdir", required=True, help="含 results.json 的目录")
    p_report.set_defaults(func=_cmd_report)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "run" and args.attempts < 1:
        parser.error("--attempts 必须 >= 1")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
