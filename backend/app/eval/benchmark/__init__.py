"""Reproducible benchmark suite (stage 7B.1.4D).

- `dataset.build_benchmark_dataset`：curated 真实公开信息 → frozen bundle；
- `experiment.run_benchmark_experiment`：三路 variant 实验（fake 离线 / real）；
- `fakes`：确定性离线模型集。

CLI：`python -m app.eval.benchmark`（dataset / run / score / report）。
"""

from app.eval.benchmark.dataset import (
    BENCHMARK_AS_OF,
    BENCHMARK_DATASET_ID,
    BENCHMARK_DATASET_VERSION,
    build_benchmark_dataset,
)
from app.eval.benchmark.experiment import (
    BenchmarkExperimentOptions,
    run_benchmark_experiment,
)

__all__ = [
    "BENCHMARK_AS_OF",
    "BENCHMARK_DATASET_ID",
    "BENCHMARK_DATASET_VERSION",
    "BenchmarkExperimentOptions",
    "build_benchmark_dataset",
    "run_benchmark_experiment",
]
