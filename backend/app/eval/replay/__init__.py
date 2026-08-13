"""Evaluation bundle rehydration（stage 7B.1.4B.1）。

Frozen Evaluation Bundle → 隔离 PostgreSQL + RawArtifactStore 的运行时复现。
"""

from app.eval.replay.contracts import (
    EVAL_REHYDRATION_POLICY_VERSION,
    RehydratedCase,
    RehydratedDocument,
)
from app.eval.replay.rehydrator import EvaluationReplayRehydrator

__all__ = [
    "EVAL_REHYDRATION_POLICY_VERSION",
    "RehydratedCase",
    "RehydratedDocument",
    "EvaluationReplayRehydrator",
]
