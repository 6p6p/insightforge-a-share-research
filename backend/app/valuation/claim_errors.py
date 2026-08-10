"""Stable error taxonomy for relative valuation claims (stage 4C.2B.1).

错误消息不包含：evidence 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码，映射自 Relative Valuation Claim 的确定性不变量。
"""

from app.valuation.errors import ValuationError


class ValuationClaimError(ValuationError):
    """ValuationClaimService 错误基类。"""

    code = "valuation_claim_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.code)


class ValuationClaimDraftError(ValuationClaimError):
    """ValuationClaimDraft 构造校验失败（语义输入不合法）。

    与 comparison not found / company mismatch / integrity / relation conflict
    错误区分：这是调用方在提供语义输入时就可被拒绝的错误。
    """

    code = "valuation_claim_draft_error"


class ValuationClaimComparisonNotFound(ValuationClaimError):
    """draft 里的 comparison_id 在 PG 中不存在。

    加载 Comparison refs 时任一缺失 → 拒绝创建，不自动修复。
    """

    code = "valuation_claim_comparison_not_found"


class ValuationClaimComparisonMismatch(ValuationClaimError):
    """Comparison 与 draft 的公司不一致。

    Comparison 只能绑定同一 company_id 下的 Claim；跨公司 → 拒绝。
    """

    code = "valuation_claim_comparison_mismatch"


class ValuationClaimAnalysisDateMismatch(ValuationClaimError):
    """Comparison.analysis_as_of 与 draft.analysis_as_of 不一致。

    全部 selected comparisons 的 analysis_as_of 必须与 claim 的 analysis_as_of
    完全一致（严格同分析时点；不自动最近日期对齐）。
    """

    code = "valuation_claim_analysis_date_mismatch"


class ValuationClaimMetricDateMismatch(ValuationClaimError):
    """全部 selected comparisons 的 metric_as_of 不一致。

    一个 claim 引用的全部 comparisons 必须使用同一 market observation date
    （严格 same-date；不自动就近交易日对齐）。
    """

    code = "valuation_claim_metric_date_mismatch"


class ValuationClaimPeerSetMismatch(ValuationClaimError):
    """全部 selected comparisons 的 peer_company_id 集合不一致。

    一个 claim 引用的全部 comparisons 必须使用**完全相同**的 peer company
    set（不做 silent intersection / union，也不自动补齐）。
    """

    code = "valuation_claim_peer_set_mismatch"


class ValuationClaimDuplicateMetric(ValuationClaimError):
    """一个 claim 内 metric_code 重复（或超过 v1 的 PE/PB/PS 三个 comparison）。"""

    code = "valuation_claim_duplicate_metric"


class ValuationClaimEvidenceCompanyMismatch(ValuationClaimError):
    """additional EvidenceCard 与 Claim 的公司不一致或证据不存在。

    additional Evidence 必须是 target 公司（draft.company_id）的真实
    EvidenceCard；缺失 / 跨公司 / peer company Evidence → 拒绝，不自动修复。
    """

    code = "valuation_claim_evidence_company_mismatch"


class ValuationClaimRelationConflict(ValuationClaimError):
    """同一 Evidence 被推导成不同 relation。

    自动展开的 source Evidence（一律 context）与 additional Evidence 之间，
    任一 Evidence 出现冲突 relation → 拒绝，**不静默选一个**。
    """

    code = "valuation_claim_relation_conflict"


class ValuationClaimCriticalEvidenceInsufficient(ValuationClaimError):
    """critical valuation Claim 的 support Comparison 来源 Evidence 不全部 eligible。

    critical 要求：每个 support Comparison 的 target Observation + 全部 peer
    Observations 的 source Evidence **全部**满足
    critical_claim_eligible_snapshot=true；additional supports 不能替代。
    """

    code = "valuation_claim_critical_evidence_insufficient"


class ValuationClaimIntegrityError(ValuationClaimError):
    """已有 fingerprint 的 valuation Claim replay 完整性校验失败。

    校验 Comparison / peer links / Observations / EvidenceCards / links /
    Profile / 自动展开 / critical policy / relation conflict / fingerprint 与
    真实数据一致；任一损坏抛此错误，**不自动 repair**（修改 = 新 Claim）。
    """

    code = "valuation_claim_integrity_error"


class ValuationClaimPersistenceFailed(ValuationClaimError):
    """Valuation Claim 持久化事务失败（已整条回滚，0 partial write）。"""

    code = "valuation_claim_persistence_failed"
