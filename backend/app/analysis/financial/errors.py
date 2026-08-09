"""Structured financial analysis error taxonomy (stage 4B.2C.2).

错误消息不包含：evidence 正文、calculation 数值细节、完整 prompt、API key、
provider raw response、DB URL、raw content。`code` 是稳定错误码。
"""


class FinancialAnalysisError(Exception):
    """结构化 Financial 分析域稳定错误基类。"""

    code = "financial_analysis_error"
    message = "financial analysis error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class FinancialAnalysisInputError(FinancialAnalysisError):
    """调用方输入不合法（research_question 为空 / calculation 数量越界 / id 非 UUID）。

    可在调用 LLM 前确定性拒绝，不触发模型调用。
    """

    code = "financial_analysis_input_error"
    message = "invalid financial analysis request"


class FinancialAnalysisCalculationNotFound(FinancialAnalysisError):
    """request 里的 calculation_id 在 PG 中不存在。

    加载 Calculation refs 时任一缺失 → 拒绝分析（**不调用 LLM**）。
    """

    code = "financial_analysis_calculation_not_found"
    message = "analysis calculation not found"


class FinancialAnalysisCalculationCompanyMismatch(FinancialAnalysisError):
    """Calculation 与 request 的公司不一致。

    Calculation 只能分析同一 company_id 下的数据；跨公司 → 拒绝（**不调用
    LLM**）。
    """

    code = "financial_analysis_calculation_company_mismatch"
    message = "analysis calculation must belong to the request company"


class FinancialAnalysisCalculationCorrupted(FinancialAnalysisError):
    """Calculation 重放完整性校验失败（上游 Observation 被篡改 / input 缺失）。

    任何 missing / company mismatch / corruption → 拒绝分析（**不调用 LLM**），
    不自动 repair。
    """

    code = "financial_analysis_calculation_corrupted"
    message = "analysis calculation integrity check failed"


class FinancialAnalysisEvidenceCompanyMismatch(FinancialAnalysisError):
    """additional Evidence 不存在或不属于 request.company_id。

    分析只基于 request 公司下的真实 Evidence；任一缺失 / 跨公司 → 拒绝。
    """

    code = "financial_analysis_evidence_company_mismatch"
    message = "analysis evidence must exist and belong to the request company"


class FinancialAnalysisMalformedOutput(FinancialAnalysisError):
    """模型返回的结构化输出不符合 FinancialAnalysisDecision schema。

    包括：字段缺失 / 类型错误 / 枚举非法、ref 格式错误、relevant=false 但
    claims 非空、relevant=true 但 claims 不在 1..3、无 support_calculation_ref。
    """

    code = "financial_analysis_malformed_output"
    message = "financial analysis output failed schema validation"


class FinancialAnalysisNumericLiteralForbidden(FinancialAnalysisError):
    """Claim statement 包含 ASCII / full-width digits / % / 中文数字 / 定量短语。

    Financial Analyst 不得在 statement 中输出任何数字形式或定量表达（定量事实
    通过 C 编号引用表达）。**不自动删数字 / 不改写 / 不让第二个 LLM 修正**；任一
    Claim 违反 → 整次分析失败（0 写）。
    """

    code = "financial_analysis_numeric_literal_forbidden"
    message = "claim statement must not contain numeric literals"


class FinancialAnalysisUnknownRef(FinancialAnalysisError):
    """模型输出引用了不存在的 C/E 编号（如只有 C1..C2 却引用 C99）。

    不做 fuzzy resolve、不自动猜 UUID；未知引用 → 整次分析失败（0 写）。
    """

    code = "financial_analysis_unknown_ref"
    message = "analysis output references unknown calculation or evidence"


class FinancialAnalysisRelationConflict(FinancialAnalysisError):
    """同一 C/E ref 在同一 Claim 内跨 relation 重复。

    与 FinancialClaimDraft 的跨 relation 不变量一致；冲突 → 整次分析失败（0 写）。
    """

    code = "financial_analysis_relation_conflict"
    message = "analysis output uses the same ref in multiple relations"


class FinancialAnalysisClaimKindPolicy(FinancialAnalysisError):
    """claim_kind 超出 Financial Analyst 允许范围（只允许 inference / risk）。

    FinancialClaimDraft（更低层 domain contract）仍支持 fact；本错误只代表
    Financial Analysis 路径拒绝了 fact / relative_valuation。
    """

    code = "financial_analysis_claim_kind_policy"
    message = "claim kind incompatible with financial analysis"


class FinancialAnalysisModelUnavailable(FinancialAnalysisError):
    """Financial analysis model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "financial_analysis_model_unavailable"
    message = "financial analysis model unavailable"
