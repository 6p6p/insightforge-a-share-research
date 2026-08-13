"""Evaluation execution persistence (stage 7B.1.3A).

只持久化 `ExecutionSpec 1:N Trial 1:N Attempt 1:N LLM Call Usage` 四层，
**不**持久化 MetricValue / ScoringSpec / HumanLabel / Judge 结果（spec U）。
"""

from app.eval.persistence.contracts import (
    VerifiedAttemptRecord,
    VerifiedExecutionSpecRecord,
    VerifiedTrialRecord,
)
from app.eval.persistence.service import EvaluationExecutionPersistenceService

__all__ = [
    "EvaluationExecutionPersistenceService",
    "VerifiedAttemptRecord",
    "VerifiedExecutionSpecRecord",
    "VerifiedTrialRecord",
]
