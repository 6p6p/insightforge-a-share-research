"""Stable error taxonomy for macro claims (stage 4C.1A).

错误消息不包含：evidence 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码，映射自 Macro Claim / Macro Transmission 的确定性不变量。
"""


class MacroClaimError(Exception):
    """MacroClaimService 错误基类。"""

    code = "macro_claim_error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.code)


class MacroClaimDraftError(MacroClaimError):
    """MacroClaimDraft 构造校验失败（语义输入不合法）。"""

    code = "macro_claim_draft_error"


class MacroClaimEvidenceNotFound(MacroClaimError):
    """draft 引用的 EvidenceCard 在 PG 中不存在。

    加载全部 evidence refs 时任一缺失 → 拒绝创建，不自动修复。
    """

    code = "macro_claim_evidence_not_found"


class MacroClaimEvidenceCompanyMismatch(MacroClaimError):
    """EvidenceCard 与 Macro Claim 的公司不一致。

    全部 Evidence（macro_driver / company_exposure / observed_effect /
    additional）必须属于同一 company_id；跨公司 → 拒绝，不自动修复。
    """

    code = "macro_claim_evidence_company_mismatch"


class MacroClaimOriginViolation(MacroClaimError):
    """传导角色与 EvidenceCard 的 origin 不一致。

    macro_driver 必须是 origin_type=macro_observation；company_exposure /
    observed_effect 必须是 origin_type=document_chunk。
    """

    code = "macro_claim_origin_violation"


class MacroClaimRelationConflict(MacroClaimError):
    """同一 Evidence 出现跨角色 / 跨 relation 重复。

    同一 Evidence 不能出现在多个传导角色，也不能同时出现在传导角色与 additional
    relation 组；additional 三组 relation 也互相排斥。违反 → 拒绝，**不静默选
    一个**。
    """

    code = "macro_claim_relation_conflict"


class MacroClaimFutureEvidence(MacroClaimError):
    """证据时间晚于 analysis_as_of。

    任一已知 source/evidence time 晚于 analysis_as_of → 拒绝创建（未来证据不能
    支撑当下的分析结论）。程序只做"不晚于"的确定性校验，不做滞后合理性判断。
    """

    code = "macro_claim_future_evidence"


class MacroClaimTemporalEvidenceInsufficient(MacroClaimError):
    """macro_driver / company_exposure 缺少可用时间判断。

    每个 macro_driver 与每个 company_exposure 都必须有至少一个可用时间
    （macro 用 Observation 期间；document 用 published_at / reporting_period_end）。
    无可用时间 → 拒绝创建，**不伪造缺失日期**。
    """

    code = "macro_claim_temporal_evidence_insufficient"


class MacroClaimCriticalEvidenceInsufficient(MacroClaimError):
    """critical Macro Claim 缺少 eligible 的传导双腿。

    critical 需要 ≥1 macro_driver eligible **且** ≥1 company_exposure eligible；
    impact_status=observed_impact 时还额外需要 ≥1 observed_effect eligible。
    additional support 不能替代两条传导腿。
    """

    code = "macro_claim_critical_evidence_insufficient"


class MacroClaimImpactStatusInsufficient(MacroClaimError):
    """impact_status 与已绑定证据不匹配（overclaim 防御）。

    observed_impact 至少需要 1 个 observed_effect Evidence；仅凭 macro_driver +
    company_exposure 不能声称"影响已经发生"。
    """

    code = "macro_claim_impact_status_insufficient"


class MacroClaimTimeAlignmentPolicy(MacroClaimError):
    """time_alignment 与 impact/importance 的确定性一致性（overclaim 防御，v2）。

    - impact_status=observed_impact 必须 time_alignment=aligned（声称"影响已
      发生"不能同时说"时间对齐不确定"）；
    - time_alignment=uncertain 只允许 impact_status=plausible_impact +
      claim_kind=risk + importance=normal（不确定 → 不能创建 critical / 不能
      声称已发生因果）。

    违反 → 拒绝创建，**不自动降级或猜 lag**。
    """

    code = "macro_claim_time_alignment_policy"


class MacroClaimIntegrityError(MacroClaimError):
    """已有 fingerprint 的 Macro Claim replay 完整性校验失败。

    校验 Claim / MacroTransmissionChain / TransmissionEvidenceLinks /
    EvidenceCards / ClaimEvidenceLinks / company / origin / analysis_as_of /
    temporal policy / critical policy / impact-status rule / additional
    relations / fingerprint 与真实数据一致；任一损坏抛此错误，**不自动 repair**
    （修改 = 新 Claim = 新 transmission）。
    """

    code = "macro_claim_integrity_error"


class MacroClaimPersistenceFailed(MacroClaimError):
    """Macro Claim 持久化事务失败（已整条回滚，0 partial write）。"""

    code = "macro_claim_persistence_failed"
