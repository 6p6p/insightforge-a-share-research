"""Stable error taxonomy for claim synthesis input (stage 4D.1A).

错误消息不包含：evidence 正文、完整 raw content、DB URL、absolute path、
UUID 集合明细。`code` 是稳定错误码，映射自 synthesis 的确定性不变量。
"""


class SynthesisError(Exception):
    """Claim Synthesis 域稳定错误基类。"""

    code = "synthesis_error"
    message = "claim synthesis error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class SynthesisDraftError(SynthesisError):
    """SynthesisInputDraft 构造校验失败（语义输入不合法）。

    与运行时隔离 / integrity / temporal 错误区分：这是调用方在提供输入时
    就可被拒绝的错误（research_question trim 非空 / claim_ids 2..50 去重）。
    """

    code = "synthesis_draft_error"
    message = "invalid claim synthesis draft"


class SynthesisRunNotFound(SynthesisError):
    """SynthesisRun 不存在（read-side：verify_synthesis_integrity 找不到 run）。

    与 integrity 错误区分：run 从未登记是"调用方传错 id"，不是"已登记 run
    被篡改"。消费方（SynthesisAnalysisService）映射为自身的 RunNotFound。
    """

    code = "synthesis_run_not_found"
    message = "synthesis run not found"


class SynthesisResearchQuestionMismatch(SynthesisError):
    """输入 Claim 的 research_question_sha256 与 synthesis draft 不一致。

    综合只能基于**同一 research question** 下产生的 Claim（spec L：每个
    claim.research_question_sha256 == draft hash），否则跨 question 混入，
    拒绝并提示调用方按 question 分组提交。
    """

    code = "synthesis_research_question_mismatch"
    message = "claim research question must match the synthesis research question"


class SynthesisCompanyMismatch(SynthesisError):
    """输入 Claim 的 company_id 与 synthesis draft 不一致。

    综合只能基于**同一公司**的 Claim（spec M：所有 claim.company_id ==
    draft.company_id），跨公司混入会破坏 company-level 综合边界。
    """

    code = "synthesis_company_mismatch"
    message = "claim company must match the synthesis company"


class SynthesisUnsupportedClaimSchema(SynthesisError):
    """Claim 的 analysis_domain / claim_schema_version 组合不受 synthesis 支持。

    gateway 按真实 analysis_domain + claim_schema_version dispatch 到
    generic / Financial / Macro / Valuation 完整性校验；无法重算 fingerprint
    （如 legacy macro v1/v2 chain 无 analysis_as_of 查询列）→ 明确拒绝，不
    猜测 / 不跳过。
    """

    code = "synthesis_unsupported_claim_schema"
    message = "claim schema is not supported by claim synthesis"


class SynthesisClaimIntegrityError(SynthesisError):
    """输入 Claim（或其 domain provenance 链）完整性校验失败。

    Claim 缺失 / domain 子表缺失 / fingerprint 与真实 persisted provenance
    重算结果不一致 / 引用的 Evidence / Calculation / Comparison 缺失。只做
    校验，**不自动 repair**（修改观点 = 新 Claim，综合输入只接受已验证的
    Claim）。
    """

    code = "synthesis_claim_integrity_error"
    message = "input claim integrity error"


class SynthesisFutureEvidence(SynthesisError):
    """输入 Claim 引用的 Evidence 在 synthesis cutoff 之后才可用。

    no-lookahead（spec O）：evidence availability（真实 provenance 解析，非
    reporting_period_end）必须 <= synthesis analysis_as_of；future 证据混入
    会让综合结论把未来信息当已发生事实。
    """

    code = "synthesis_future_evidence"
    message = "input claim evidence is available after the synthesis cutoff"


class SynthesisTemporalEvidenceInsufficient(SynthesisError):
    """输入 Claim 的 Evidence 无法解析 availability 时间（provenance 缺失）。

    macro 卡缺 snapshot.fetched_at / document 卡缺 source.published_at 且
    source 缺失或 acquired_at 缺失 → 无法证明"何时可知"，拒绝综合（不伪造
    缺失日期）。
    """

    code = "synthesis_temporal_evidence_insufficient"
    message = "input claim evidence availability cannot be resolved"


class SynthesisIntegrityError(SynthesisError):
    """已有 fingerprint 的 SynthesisRun replay 完整性校验失败。

    校验 run 字段 / 精确 claim set / synthesis_fingerprint 与真实 Claim +
    domain provenance + Evidence 重算结果一致；任一损坏抛此错误，
    **不自动 repair**（输入任一变化 = 新 run，旧 run 保留）。
    """

    code = "synthesis_integrity_error"
    message = "synthesis run replay integrity error"


class SynthesisPersistenceFailed(SynthesisError):
    """SynthesisRun 持久化事务失败（DB 层错误）。"""

    code = "synthesis_persistence_failed"
    message = "synthesis persistence failed"
