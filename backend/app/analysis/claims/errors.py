"""Structured claim analysis error taxonomy (stage 4B.1).

错误消息不包含：evidence 正文、完整 prompt、API key、provider raw response、
DB URL、raw content。`code` 是稳定错误码。
"""


class ClaimAnalysisError(Exception):
    """结构化 Claim 分析域稳定错误基类。"""

    code = "claim_analysis_error"
    message = "claim analysis error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ClaimAnalysisInputError(ClaimAnalysisError):
    """调用方输入不合法（如 research_question 为空 / evidence 数量越界 / id 非 UUID）。

    可在调用 LLM 前确定性拒绝，不触发模型调用。
    """

    code = "claim_analysis_input_error"
    message = "invalid claim analysis request"


class ClaimAnalysisDomainNotReady(ClaimAnalysisError):
    """analysis_domain 尚未就绪（4B.1 只支持 business / event / risk）。

    financial / macro / valuation 未到验收门槛：financial 需确定性财务计算、
    macro 需专用传导契约、valuation 依赖后续财务/同业数据；调用这些 domain →
    拒绝，不提前实现。
    """

    code = "claim_analysis_domain_not_ready"
    message = "analysis domain not ready in 4B.1 (only business/event/risk)"


class ClaimAnalysisEvidenceCompanyMismatch(ClaimAnalysisError):
    """Evidence 不存在或不属于 request.company_id。

    分析只基于 request 公司下的真实 Evidence；任一缺失 / 跨公司 → 拒绝，不自动修复。
    """

    code = "claim_analysis_evidence_company_mismatch"
    message = "analysis evidence must exist and belong to the request company"


class ClaimAnalysisUnknownEvidenceRef(ClaimAnalysisError):
    """模型输出引用了证据包中不存在的 E 编号（如包只有 E1..E3 却引用 E99）。

    不做 fuzzy resolve、不自动猜 UUID；未知引用 → 整次分析失败（0 写）。
    """

    code = "claim_analysis_unknown_evidence_ref"
    message = "analysis output references unknown evidence"


class ClaimAnalysisRelationConflict(ClaimAnalysisError):
    """同一 evidence ref 在同一 Claim 内跨 relation 重复（supports+contradicts 等）。

    与 ClaimDraft 的 v1 跨 relation 不变量一致；冲突 → 整次分析失败（0 写）。
    """

    code = "claim_analysis_relation_conflict"
    message = "analysis output uses the same evidence ref in multiple relations"


class ClaimAnalysisMalformedOutput(ClaimAnalysisError):
    """模型返回的结构化输出不符合 ClaimAnalysisDecision schema。

    包括：字段缺失/类型错误/枚举非法、ref 格式错误、relevant=false 但 claims
    非空、relevant=true 但 claims 不在 1..5、无 support_ref。
    """

    code = "claim_analysis_malformed_output"
    message = "claim analysis output failed schema validation"


class ClaimAnalysisModelUnavailable(ClaimAnalysisError):
    """Claim analysis model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "claim_analysis_model_unavailable"
    message = "claim analysis model unavailable"


class ClaimAnalysisDomainKindIncompatible(ClaimAnalysisError):
    """claim_kind 与分析 domain 不兼容（如 relative_valuation 进入 4B.1 domain）。"""

    code = "claim_analysis_domain_kind_incompatible"
    message = "claim kind incompatible with analysis domain"
