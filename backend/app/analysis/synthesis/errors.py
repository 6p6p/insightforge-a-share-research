"""Structured claim synthesis analysis error taxonomy (stage 4D.1B).

错误消息不包含：evidence 正文、provider raw response、API key、完整 prompt、
DB URL、raw content。`code` 是稳定错误码。
"""


class SynthesisAnalysisError(Exception):
    """结构化综合分析域稳定错误基类。"""

    code = "synthesis_analysis_error"
    message = "synthesis analysis error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class SynthesisAnalysisInputError(SynthesisAnalysisError):
    """调用方输入不合法（synthesis_id 非 UUID 等）。

    可在调用 LLM 前确定性拒绝，不触发模型调用。
    """

    code = "synthesis_analysis_input_error"
    message = "invalid synthesis analysis request"


class SynthesisAnalysisRunNotFound(SynthesisAnalysisError):
    """request 引用的 SynthesisRun 在 PG 中不存在。

    综合分析只针对已登记的综合输入集；run 缺失 → 拒绝分析（**不调用 LLM**）。
    """

    code = "synthesis_analysis_run_not_found"
    message = "synthesis run not found"


class SynthesisAnalysisResultNotFound(SynthesisAnalysisError):
    """verify_result_integrity 引用的 SynthesisResult 在 PG 中不存在。

    Stage 5A：ReportOutline 只接受已登记的综合结果；result 缺失 → 拒绝派生
    提纲（不猜结果 / 不自动创建）。"""

    code = "synthesis_analysis_result_not_found"
    message = "synthesis result not found"


class SynthesisResultIntegrityError(SynthesisAnalysisError):
    """已登记 SynthesisResult 的 read-side 完整性校验失败（Stage 5A）。

    verify_result_integrity 校验：run 完整（委托 verify_synthesis_integrity）、
    result schema / analyst 身份与当前常量一致、payload 可解析、
    resolved claim IDs 全属 exact input set、重算 result_fingerprint 与
    persisted 一致；任一损坏抛此错误，**不自动 repair**（结果不可变，损坏 =
    数据被篡改 → 拒绝派生提纲）。
    """

    code = "synthesis_result_integrity_error"
    message = "synthesis result integrity error"


class SynthesisAnalysisMalformedOutput(SynthesisAnalysisError):
    """模型返回的结构化输出不符合 SynthesisAnalysisOutput schema。

    包括：字段缺失 / 类型错误 / 枚举非法、summary/themes/rationale 空、
    duplicate/conflict 组 <2 个 ref、canonical_ref 不在组内。
    """

    code = "synthesis_analysis_malformed_output"
    message = "synthesis analysis output failed schema validation"


class SynthesisAnalysisUnknownRef(SynthesisAnalysisError):
    """模型输出引用了不存在的 C 编号（如只有 C1..C4 却引用 C99）。

    不做 fuzzy resolve、不自动猜 claim；未知引用 → 整次分析失败（0 写）。
    """

    code = "synthesis_analysis_unknown_ref"
    message = "synthesis analysis output references unknown claim alias"


class SynthesisAnalysisNoCherryPicking(SynthesisAnalysisError):
    """claim_roles 未恰好覆盖每条 input Claim（no-cherry-picking 硬边界）。

    LLM 必须对每条输入 Claim 恰当地分配一个角色——缺漏 / 重复 / 自造都不允许；
    违反 → 整次分析失败（0 写），**不静默补齐**。
    """

    code = "synthesis_analysis_no_cherry_picking"
    message = "synthesis analysis claim roles must cover every input claim exactly once"


class SynthesisAnalysisModelUnavailable(SynthesisAnalysisError):
    """Synthesis analysis model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "synthesis_analysis_model_unavailable"
    message = "synthesis analysis model unavailable"


class SynthesisAnalysisPersistenceFailed(SynthesisAnalysisError):
    """综合结果持久化事务失败（已整条回滚，0 partial write）。"""

    code = "synthesis_analysis_persistence_failed"
    message = "synthesis analysis persistence failed"
