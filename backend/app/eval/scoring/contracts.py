"""Scoring persistence read models (stage 7B.1.3B).

`VerifiedScoreRunRecord` / `VerifiedMetricValueRecord`：评分持久化的已验证读模型
（从 DB 行加载 → 重校验 fingerprint → 返回；**不**信 DB JSONB 原始内容）。
"""

from dataclasses import dataclass
from decimal import Decimal
from uuid import UUID

from app.eval.contracts import EvalScoringSpec


@dataclass(frozen=True)
class VerifiedMetricValueRecord:
    """一条已验证的 MetricValue 投影（不含 fingerprint / created_at）。"""

    metric_name: str
    metric_version: int
    status: str
    value: Decimal | None
    numerator: Decimal | None
    denominator: Decimal | None
    sample_count: int
    reason_code: str | None


@dataclass(frozen=True)
class VerifiedScoreRunRecord:
    """一次已验证的 score run（spec 重校验 + metric values 投影）。"""

    score_run_id: UUID
    execution_id: UUID
    spec: EvalScoringSpec
    metric_values: tuple[VerifiedMetricValueRecord, ...]
