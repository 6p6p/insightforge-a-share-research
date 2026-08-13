"""Stage 7B.1.1B snapshot materializer（PG + RawArtifactStore → frozen bundle）。"""

from app.eval.materialization.contracts import (
    EvalCaseMaterializationSpec,
    MaterializedEvalCase,
    StructuredArtifactSelection,
)
from app.eval.materialization.service import EvaluationSnapshotMaterializer

__all__ = [
    "EvalCaseMaterializationSpec",
    "EvaluationSnapshotMaterializer",
    "MaterializedEvalCase",
    "StructuredArtifactSelection",
]
