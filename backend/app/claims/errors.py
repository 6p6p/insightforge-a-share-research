"""Stable error taxonomy for claims (stage 4A).

错误消息不包含：evidence 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码，映射自 Claim 的确定性不变量。
"""


class ClaimError(Exception):
    """Claim 域稳定错误基类。"""

    code = "claim_error"
    message = "claim error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ClaimDraftError(ClaimError):
    """ClaimDraft 构造校验失败（语义输入不合法）。

    与 evidence insufficient / company mismatch / integrity 错误区分：这是
    调用方在提供语义输入时就可被拒绝的错误。
    """

    code = "claim_draft_error"
    message = "invalid claim draft"


class ClaimEvidenceInsufficient(ClaimError):
    """Claim 缺少最小支持证据（至少 1 个 supports Evidence）。

    结构规则，不做语义判断：statement 是否真的被 Evidence 支持由 LLM
    Analyst / later Auditor 判断。
    """

    code = "claim_evidence_insufficient"
    message = "claim requires at least one supporting evidence"


class ClaimCriticalEvidenceInsufficient(ClaimError):
    """critical Claim 缺少 eligible supports Evidence。

    critical 至少需要 1 个 supports Evidence 满足
    critical_claim_eligible_snapshot = true。不因 confidence=high 放宽来源
    政策；不因多个 Tier-3 Evidence 自动推断为 critical eligible。
    """

    code = "claim_critical_evidence_insufficient"
    message = "critical claim requires at least one critical-claim-eligible supporting evidence"


class ClaimEvidenceCompanyMismatch(ClaimError):
    """EvidenceCard 与 Claim 的公司不一致或证据不存在。

    Claim 只能绑定同一 company_id 下的 Evidence；任一缺失 / 跨公司 →
    拒绝创建，不自动修复。
    """

    code = "claim_evidence_company_mismatch"
    message = "claim evidence must exist and belong to the claim company"


class MacroClaimTransmissionEvidenceInsufficient(ClaimError):
    """macro Claim 缺少传导链证据结构。

    analysis_domain=macro 需要 ≥1 macro_observation support **且** ≥1
    document_chunk Evidence（supports 或 context，体现公司暴露 / 公司经营事实）。
    只验证"证据结构具备传导链材料"，不判断实际因果是否成立。
    """

    code = "macro_claim_transmission_evidence_insufficient"
    message = "macro claim requires a macro support plus a company document evidence"


class ClaimIntegrityError(ClaimError):
    """已有 fingerprint 的 Claim replay 完整性校验失败。

    校验 statement/enums/company/question hash/analyst identity/link 数量/
    relations/Evidence IDs/critical rule/macro rule/fingerprint 与真实
    Evidence 一致；任一损坏抛此错误，**不自动 repair**（修改观点 = 新 Claim）。
    """

    code = "claim_integrity_error"
    message = "claim replay integrity error"


class ClaimPersistenceFailed(ClaimError):
    """Claim 持久化事务失败（DB 层错误）。"""

    code = "claim_persistence_failed"
    message = "claim persistence failed"
