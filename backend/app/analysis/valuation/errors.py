"""Structured Relative Valuation Analysis error taxonomy (stage 4C.2B.2).

错误消息不包含：evidence 正文、provider raw response、API key、完整 prompt、
DB URL、raw content、UUID alias 映射。`code` 是稳定错误码。
"""


class ValuationAnalysisError(Exception):
    """结构化 Relative Valuation 分析域稳定错误基类。"""

    code = "valuation_analysis_error"
    message = "valuation analysis error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class ValuationAnalysisInputError(ValuationAnalysisError):
    """调用方输入不合法（research_question 为空 / comparison 数量越界 / id 非 UUID）。

    可在调用 LLM 前确定性拒绝，不触发模型调用。
    """

    code = "valuation_analysis_input_error"
    message = "invalid valuation analysis request"


class ValuationAnalysisComparisonNotFound(ValuationAnalysisError):
    """request 里的 comparison_id 在 PG 中不存在。

    加载 Comparison refs 时任一缺失 → 拒绝分析（**不调用 LLM**）。
    """

    code = "valuation_analysis_comparison_not_found"
    message = "analysis comparison not found"


class ValuationAnalysisComparisonCompanyMismatch(ValuationAnalysisError):
    """Comparison 与 request 的公司不一致。

    分析只基于 request 公司下的真实 Comparison；任一跨公司 → 拒绝（**不调用 LLM**）。
    """

    code = "valuation_analysis_comparison_company_mismatch"
    message = "analysis comparison must belong to the request company"


class ValuationAnalysisComparisonCorrupted(ValuationAnalysisError):
    """Comparison 的 persisted state 损坏（verify_comparison_integrity 抛
    ValuationIntegrityError）。

    属于数据损坏，**不自动 repair**，拒绝分析（**不调用 LLM**）。
    """

    code = "valuation_analysis_comparison_corrupted"
    message = "analysis comparison provenance corrupted"


class ValuationAnalysisInputInvalid(ValuationAnalysisError):
    """跨 comparison 一致性失败（analysis_as_of / metric_as_of / peer set / metric 唯一性）。

    复用 `app.valuation.claim_policy.check_comparison_set_consistency` 的确定性
    策略；任一违反 → 拒绝分析（**不调用 LLM**）。
    """

    code = "valuation_analysis_input_invalid"
    message = "analysis comparison set violates consistency policy"


class ValuationAnalysisMalformedOutput(ValuationAnalysisError):
    """模型返回的结构化输出不符合 ValuationAnalysisDecision schema。

    包括：字段缺失 / 类型错误 / 枚举非法、V ref 格式错误、relevant=false 但
    assessment/refs 非空、relevant=true 缺 assessment/confidence/importance、
    relevant=true 但 support refs 为空。
    """

    code = "valuation_analysis_malformed_output"
    message = "valuation analysis output failed schema validation"


class ValuationAnalysisModelUnavailable(ValuationAnalysisError):
    """Valuation analysis model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "valuation_analysis_model_unavailable"
    message = "valuation analysis model unavailable"


class ValuationAnalysisUnknownRef(ValuationAnalysisError):
    """模型输出引用了不存在的 V 编号（如只有 V1..V3 却引用 V99）。

    不做 fuzzy resolve、不自动猜 UUID；未知引用 → 整次分析失败（0 写）。
    """

    code = "valuation_analysis_unknown_ref"
    message = "analysis output references unknown comparison"


class ValuationAnalysisRelationConflict(ValuationAnalysisError):
    """同一 V ref 被归入多个 relation（supports / contradicts / context 互斥）。

    与 ValuationClaimDraft 的跨 relation 不变量一致；冲突 → 整次分析失败（0 写）。
    """

    code = "valuation_analysis_relation_conflict"
    message = "analysis output uses the same comparison in multiple relations"


class ValuationAnalysisComparisonOmitted(ValuationAnalysisError):
    """模型输出遗漏了某个 input comparison（no cherry-picking 硬边界）。

    relevant=true 时 support ∪ contradict ∪ context 必须**恰好等于** request 的
    全部 comparison aliases；任何 input comparison 未被归入任意一组 → 整次失败
    （0 写），禁止 fuzzy / 猜 UUID / 静默忽略。
    """

    code = "valuation_analysis_comparison_omitted"
    message = "analysis output omitted an input comparison"


class ValuationAnalysisDirectionConflict(ValuationAnalysisError):
    """assessment 与 support Comparison 的 premium 符号显然相反。

    relative_high 要求全部 support premium > 0；relative_low 要求全部 support
    premium < 0。**不写数值 threshold**（premium>20%→high 之类属于 Analyst
    judgement）；只拒绝方向性矛盾 → 整次失败（0 写），禁止自动改写 assessment。
    """

    code = "valuation_analysis_direction_conflict"
    message = "analysis assessment contradicts support comparison premium sign"


class ValuationAnalysisMixedEvidenceInsufficient(ValuationAnalysisError):
    """assessment=mixed 但 support 中缺少正 / 负 premium 之一。

    mixed 要求 support 中至少一个 premium > 0 **且**至少一个 premium < 0（否则
    Analyst 不应强行给出 mixed）；违反 → 整次失败（0 写）。
    """

    code = "valuation_analysis_mixed_evidence_insufficient"
    message = "mixed assessment requires both positive and negative support premiums"


class ValuationAnalysisUncertainImportancePolicy(ValuationAnalysisError):
    """assessment=uncertain 但 importance=critical。

    uncertain 不设方向 threshold，但 importance 必须 normal（不确定性判断不能被
    标注为 critical）；违反 → 整次失败（0 写）。
    """

    code = "valuation_analysis_uncertain_importance_policy"
    message = "uncertain assessment requires normal importance"


class ValuationAnalysisClaimDraftError(ValuationAnalysisError):
    """构造 ValuationClaimDraft 失败（确定性 statement / 枚举映射问题）。

    正常路径不会触发；作为防御性兜底（构造失败 → 整次失败 0 写）。
    """

    code = "valuation_analysis_claim_draft_error"
    message = "valuation claim draft construction failed"
