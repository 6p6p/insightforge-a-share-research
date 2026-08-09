"""Stable error taxonomy for financial claims (stage 4B.2C.1).

错误消息不包含：evidence 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码，映射自 Financial Claim 的确定性不变量。
"""


class FinancialClaimError(Exception):
    """FinancialClaimService 错误基类。"""

    code = "financial_claim_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.code)


class FinancialClaimDraftError(FinancialClaimError):
    """FinancialClaimDraft 构造校验失败（语义输入不合法）。

    与 calculation not found / company mismatch / integrity / relation
    conflict 错误区分：这是调用方在提供语义输入时就可被拒绝的错误。
    """

    code = "financial_claim_draft_error"


class FinancialClaimCalculationNotFound(FinancialClaimError):
    """draft 里的 calculation_id 在 PG 中不存在。

    加载 Calculation refs 时任一缺失 → 拒绝创建，不自动修复。
    """

    code = "financial_claim_calculation_not_found"


class FinancialClaimCalculationMismatch(FinancialClaimError):
    """Calculation 与 draft 的公司不一致。

    Calculation 只能绑定同一 company_id 下的 Claim；跨公司 → 拒绝。
    """

    code = "financial_claim_calculation_mismatch"


class FinancialClaimEvidenceCompanyMismatch(FinancialClaimError):
    """EvidenceCard 与 Claim 的公司不一致或证据不存在。

    自动展开的 source Evidence 或 additional Evidence 任一缺失 / 跨公司 →
    拒绝创建，不自动修复。
    """

    code = "financial_claim_evidence_company_mismatch"


class FinancialClaimRelationConflict(FinancialClaimError):
    """同一 Evidence 被推导成不同 relation。

    自动展开（多个 Calculations 各自推导）与 additional Evidence 之间，任一
    Evidence 出现冲突 relation → 拒绝，**不静默选一个**。
    """

    code = "financial_claim_relation_conflict"


class FinancialClaimCriticalEvidenceInsufficient(FinancialClaimError):
    """critical Financial Claim 缺少 eligible supports Evidence。

    复用 ClaimService 的 source policy：critical 至少需要 1 个**最终 supports**
    Evidence 满足 critical_claim_eligible_snapshot=true（自动展开 + additional
    合并后的最终链路；FinancialCalculation 本身不能升级 source authority）。
    """

    code = "financial_claim_critical_evidence_insufficient"


class FinancialClaimIntegrityError(FinancialClaimError):
    """已有 fingerprint 的 Financial Claim replay 完整性校验失败。

    校验 Calculation / inputs / Observations / EvidenceCards / links /
    自动展开 / critical policy / relation conflict / v2 fingerprint 与真实
    数据一致；任一损坏抛此错误，**不自动 repair**（修改 = 新 Claim）。
    """

    code = "financial_claim_integrity_error"


class FinancialClaimPersistenceFailed(FinancialClaimError):
    """Financial Claim 持久化事务失败（已整条回滚，0 partial write）。"""

    code = "financial_claim_persistence_failed"
