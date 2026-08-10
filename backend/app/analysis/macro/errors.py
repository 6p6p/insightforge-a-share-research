"""Structured macro context analysis error taxonomy (stage 4C.1B).

错误消息不包含：evidence 正文、provider raw response、API key、完整 prompt、
DB URL、raw content。`code` 是稳定错误码。
"""


class MacroAnalysisError(Exception):
    """结构化 Macro 分析域稳定错误基类。"""

    code = "macro_analysis_error"
    message = "macro analysis error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class MacroAnalysisInputError(MacroAnalysisError):
    """调用方输入不合法（research_question 为空 / 证据数量越界 / id 非 UUID / 两池重叠）。

    可在调用 LLM 前确定性拒绝，不触发模型调用。
    """

    code = "macro_analysis_input_error"
    message = "invalid macro analysis request"


class MacroAnalysisEvidenceNotFound(MacroAnalysisError):
    """request 里的 evidence 在 PG 中不存在。

    加载 evidence 时任一缺失 → 拒绝分析（**不调用 LLM**）。
    """

    code = "macro_analysis_evidence_not_found"
    message = "analysis evidence not found"


class MacroAnalysisEvidenceCompanyMismatch(MacroAnalysisError):
    """Evidence 与 request 的公司不一致。

    分析只基于 request 公司下的真实 Evidence；任一跨公司 → 拒绝（**不调用 LLM**）。
    """

    code = "macro_analysis_evidence_company_mismatch"
    message = "analysis evidence must belong to the request company"


class MacroAnalysisEvidenceCorrupted(MacroAnalysisError):
    """Evidence 的 provenance 链缺失（snapshot / observation / series / source 行缺失）。

    属于数据损坏，**不自动 repair**，拒绝分析（**不调用 LLM**）。
    """

    code = "macro_analysis_evidence_corrupted"
    message = "analysis evidence provenance chain corrupted"


class MacroAnalysisOriginViolation(MacroAnalysisError):
    """macro_driver 不满足 v3 资格，或 company evidence 不是 document_chunk。"""

    code = "macro_analysis_origin_violation"
    message = "analysis evidence fails origin role policy"


class MacroAnalysisTemporalEvidenceInsufficient(MacroAnalysisError):
    """Evidence availability 无法解析（全部 provenance 时间为 NULL）。

    不伪造缺失日期；拒绝分析（**不调用 LLM**）。
    """

    code = "macro_analysis_temporal_evidence_insufficient"
    message = "analysis evidence availability unresolvable"


class MacroAnalysisFutureEvidence(MacroAnalysisError):
    """Evidence availability 晚于 analysis_as_of（no-lookahead 硬边界）。

    任何未来 Evidence → 拒绝分析（**不调用 LLM**）。
    """

    code = "macro_analysis_future_evidence"
    message = "analysis evidence is future beyond analysis cutoff"


class MacroAnalysisMalformedOutput(MacroAnalysisError):
    """模型返回的结构化输出不符合 MacroAnalysisDecision schema。

    包括：字段缺失 / 类型错误 / 枚举非法、ref 格式错误（非 M/E 编号）、
    relevant=false 但 claims 非空、relevant=true 但 claims 不在 1..3、
    缺 macro_driver_ref / company_exposure_ref、overclaim contract 违反。
    """

    code = "macro_analysis_malformed_output"
    message = "macro analysis output failed schema validation"


class MacroAnalysisNumericLiteralForbidden(MacroAnalysisError):
    """Claim statement 包含 ASCII/full-width digits / % / 中文数字 / 定量短语。

    Macro Analyst 不得在 statement 中输出任何数字形式或定量表达（定量事实通过
    M/E 编号引用表达）。**不自动删数字 / 不改写 / 不让第二个 LLM 修正**；任一
    Claim 违反 → 整次分析失败（0 写）。
    """

    code = "macro_analysis_numeric_literal_forbidden"
    message = "claim statement must not contain numeric literals"


class MacroAnalysisUnknownRef(MacroAnalysisError):
    """模型输出引用了不存在的 M/E 编号（如只有 M1..M2 却引用 M99）。

    不做 fuzzy resolve、不自动猜 UUID；未知引用 → 整次分析失败（0 写）。
    """

    code = "macro_analysis_unknown_ref"
    message = "analysis output references unknown macro or company evidence"


class MacroAnalysisRelationConflict(MacroAnalysisError):
    """同一 M/E ref 在同一 Claim 内跨 relation 重复。

    与 MacroClaimDraft 的跨 relation 不变量一致；冲突 → 整次分析失败（0 写）。
    """

    code = "macro_analysis_relation_conflict"
    message = "analysis output uses the same ref in multiple relations"


class MacroAnalysisOverclaimPolicy(MacroAnalysisError):
    """overclaim contract 违反：observed_impact 缺 observed_effect /
    time_alignment=uncertain 的组合超出 plausible + risk + normal。"""

    code = "macro_analysis_overclaim_policy"
    message = "analysis output violates overclaim contract"


class MacroAnalysisClaimKindPolicy(MacroAnalysisError):
    """claim_kind 超出 Macro Analyst 允许范围（只允许 inference / risk）。"""

    code = "macro_analysis_claim_kind_policy"
    message = "claim kind incompatible with macro analysis"


class MacroAnalysisModelUnavailable(MacroAnalysisError):
    """Macro analysis model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "macro_analysis_model_unavailable"
    message = "macro analysis model unavailable"
