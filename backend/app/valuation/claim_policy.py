"""Shared relative-valuation claim deterministic policy (stage 4C.2B.2).

ValuationClaimService（4C.2B.1）与 ValuationAnalysisService（4C.2B.2）**共用**
同一套跨 comparison 确定性一致性规则，**禁止各自复制** peer-set / metric
uniqueness / same-date / analysis_as_of 两套策略；analysis 侧的 direction /
uncertain-importance 策略也集中在本模块（同为 valuation Claim 的确定性策略）。

本模块为**纯函数**（不访问 DB）：只校验调用方传入的投影，抛
`ValuationClaimPolicyError`（带稳定 `reason`）。两个 Service 各自把 reason 映射
到自己的稳定错误域（ValuationClaimError / ValuationAnalysisError），**不在本模块
引入 analysis 依赖**（保持 valuation 领域层不被 analysis 层反向依赖）。
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from app.valuation.claim_contracts import (
    MAX_VALUATION_COMPARISONS_PER_CLAIM,
    ValuationClaimAssessment,
    ValuationClaimImportance,
)
from app.valuation.errors import ValuationError


class ValuationClaimPolicyReason(StrEnum):
    """跨 comparison 一致性 / 方向 / importance 策略失败原因（稳定 code，供 Service 映射）。"""

    ANALYSIS_DATE_MISMATCH = "analysis_date_mismatch"
    METRIC_DATE_MISMATCH = "metric_date_mismatch"
    PEER_SET_MISMATCH = "peer_set_mismatch"
    DUPLICATE_METRIC = "duplicate_metric"
    TOO_MANY_COMPARISONS = "too_many_comparisons"
    DIRECTION_CONFLICT = "direction_conflict"
    MIXED_EVIDENCE_INSUFFICIENT = "mixed_evidence_insufficient"
    UNCERTAIN_IMPORTANCE_POLICY = "uncertain_importance_policy"


class ValuationClaimPolicyError(ValuationError):
    """comparison 集合违反确定性策略（reason 稳定，message 可读、不泄漏内部细节）。"""

    code = "valuation_claim_policy_error"

    def __init__(self, reason: ValuationClaimPolicyReason, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message if message is not None else reason.value)


@dataclass(frozen=True)
class ComparisonProjection:
    """一次 comparison 的跨集合一致性投影（metric / date / peer set）。"""

    metric_code: str
    metric_as_of: date
    analysis_as_of: date
    peer_companies: frozenset[UUID]


def check_comparison_set_consistency(
    *,
    expected_analysis_as_of: date,
    comparisons: list[ComparisonProjection],
    max_comparisons: int = MAX_VALUATION_COMPARISONS_PER_CLAIM,
) -> None:
    """跨 comparison 确定性一致性策略（Analysis / Claim 共用）。

    - 每个 comparison.analysis_as_of == expected_analysis_as_of
      （ANALYSIS_DATE_MISMATCH，严格同分析时点，不自动对齐）；
    - 全部 metric_as_of 相同（METRIC_DATE_MISMATCH，严格 same-date，不就近对齐）；
    - 全部 peer company set 完全相同（PEER_SET_MISMATCH，不做 silent
      intersection / union）；
    - metric_code 不得重复（DUPLICATE_METRIC，v1 最多 PE / PB / PS）；
    - 数量 <= max_comparisons（TOO_MANY_COMPARISONS）。

    任一违反 → `ValuationClaimPolicyError`（纯校验，无 DB）。
    """
    if len(comparisons) > max_comparisons:
        raise ValuationClaimPolicyError(ValuationClaimPolicyReason.TOO_MANY_COMPARISONS)
    metric_codes: set[str] = set()
    metric_as_ofs: set[date] = set()
    peer_sets: set[frozenset[UUID]] = set()
    for projection in comparisons:
        if projection.analysis_as_of != expected_analysis_as_of:
            raise ValuationClaimPolicyError(ValuationClaimPolicyReason.ANALYSIS_DATE_MISMATCH)
        metric_codes.add(projection.metric_code)
        metric_as_ofs.add(projection.metric_as_of)
        peer_sets.add(projection.peer_companies)
    if len(metric_as_ofs) > 1:
        raise ValuationClaimPolicyError(ValuationClaimPolicyReason.METRIC_DATE_MISMATCH)
    if len(peer_sets) > 1:
        raise ValuationClaimPolicyError(ValuationClaimPolicyReason.PEER_SET_MISMATCH)
    if len(metric_codes) != len(comparisons):
        raise ValuationClaimPolicyError(ValuationClaimPolicyReason.DUPLICATE_METRIC)


def check_assessment_direction_policy(
    *,
    assessment: ValuationClaimAssessment,
    support_premiums: list[Decimal],
) -> None:
    """Assessment 与 support Comparison 的 premium 符号一致性（无 hidden thresholds）。

    只拒绝**显然方向相反**的 relation，**不写数值 threshold**（premium>20%→high
    之类属于 Analyst judgement，不是程序规则）：

    - relative_high：全部 support premium 必须 > 0（否则 DIRECTION_CONFLICT）；
    - relative_low：全部 support premium 必须 < 0（否则 DIRECTION_CONFLICT）；
    - mixed：support 中必须至少一个 premium > 0 **且**至少一个 premium < 0
      （否则 MIXED_EVIDENCE_INSUFFICIENT）——PE/PB/PS 信号冲突时 Analyst 应选
      mixed / uncertain，不强行统一方向；
    - broadly_in_line：**不设** deterministic premium threshold（属于 Analyst
      judgement）；
    - uncertain：不做方向 threshold（但 importance 必须 normal，见
      `check_uncertain_importance_policy`）。

    注意：contradict / context 允许任意 sign（它们是反证 / 背景）。
    """
    if assessment == ValuationClaimAssessment.RELATIVE_HIGH:
        if not support_premiums or any(p <= 0 for p in support_premiums):
            raise ValuationClaimPolicyError(
                ValuationClaimPolicyReason.DIRECTION_CONFLICT,
                "relative_high 要求全部 support comparison 的 premium 为正",
            )
    elif assessment == ValuationClaimAssessment.RELATIVE_LOW:
        if not support_premiums or any(p >= 0 for p in support_premiums):
            raise ValuationClaimPolicyError(
                ValuationClaimPolicyReason.DIRECTION_CONFLICT,
                "relative_low 要求全部 support comparison 的 premium 为负",
            )
    elif assessment == ValuationClaimAssessment.MIXED:
        if (
            not support_premiums
            or not any(p > 0 for p in support_premiums)
            or not any(p < 0 for p in support_premiums)
        ):
            raise ValuationClaimPolicyError(
                ValuationClaimPolicyReason.MIXED_EVIDENCE_INSUFFICIENT,
                "mixed 要求 support 中同时存在正与负 premium 的比较",
            )


def check_uncertain_importance_policy(
    *,
    assessment: ValuationClaimAssessment,
    importance: ValuationClaimImportance,
) -> None:
    """uncertain + critical 拒绝（UNCERTAIN_IMPORTANCE_POLICY）。

    assessment=uncertain 时不设方向 threshold，但**importance 必须 normal**——
    不确定性判断不能被标注为 critical（critical 的 Claim 需要明确结论）。
    """
    if (
        assessment == ValuationClaimAssessment.UNCERTAIN
        and importance != ValuationClaimImportance.NORMAL
    ):
        raise ValuationClaimPolicyError(
            ValuationClaimPolicyReason.UNCERTAIN_IMPORTANCE_POLICY,
            "uncertain assessment 要求 importance=normal",
        )
