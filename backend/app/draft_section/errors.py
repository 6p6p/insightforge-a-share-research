"""Evidence-bound section writer error taxonomy (stage 5B).

错误消息不包含：evidence 正文、完整 raw content、DB URL、UUID 集合明细、raw
provider response。`code` 是稳定错误码。integrity / not-found 错误由上游
`ReportOutlineService.verify_outline_integrity` 抛出（`ReportOutlineIntegrityError`
/ `ReportOutlineNotFound` / `SynthesisResultIntegrityError`）并原样向上传播，
本模块只定义 Writer 起草层的协调错误。
"""


class DraftSectionError(Exception):
    """Draft Section 域稳定错误基类。"""

    code = "draft_section_error"
    message = "draft section error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class DraftSectionInputError(DraftSectionError):
    """调用方输入不合法（outline_id 非 UUID / section_id 为空等）。

    可在调用 LLM 前确定性拒绝，不触发模型调用。
    """

    code = "draft_section_input_error"
    message = "invalid draft section request"


class DraftSectionNotFound(DraftSectionError):
    """Outline 中不存在 request.section_id 对应的 section。

    Writer 只消费已登记提纲的已注册 section；缺失 → 拒绝起草（不猜 section /
    不自动创建）。"""

    code = "draft_section_not_found"
    message = "draft section not found"


class DraftSectionIntegrityError(DraftSectionError):
    """replay 校验失败：同 writer_input_fingerprint 的既有草稿行与本次输入不一致。

    writer_input_fingerprint 已覆盖 outline / section 身份 / allowed
    Claim/Evidence / conflict-gap 数据 / writer 身份；命中同指纹却 payload /
    ID 集合不符 → 数据被篡改 → 拒绝（**不自动 repair**）。
    """

    code = "draft_section_integrity_error"
    message = "draft section replay integrity error"


class DraftSectionMalformedOutput(DraftSectionError):
    """模型返回的结构化输出不符合 WriterDecision / ParagraphCandidate schema。

    包括：字段缺失 / 类型错误、paragraph 数量越界（1..10）、text 空或超长、
    paragraph 缺 claim_refs / evidence_refs、ref 格式非法（非 C/E/X/G 编号）。
    """

    code = "draft_section_malformed_output"
    message = "draft section output failed schema validation"


class DraftSectionUnknownRef(DraftSectionError):
    """模型输出引用了不存在的编号（如只有 E1..E3 却引用 E99）。

    不做 fuzzy resolve、不自动猜；未知引用 → 整次起草失败（0 写）。
    """

    code = "draft_section_unknown_ref"
    message = "draft section output references unknown alias"


class DraftSectionCrossSectionRef(DraftSectionError):
    """模型输出引用了**属于其他 section** 的 Claim（在本 section 之外）。

    Writer 只能使用 Outline 允许的 Claim；引用本 section 不包含但在合成输入集
    内的 Claim → 模型扩大 Outline scope → 拒绝（**不自动扩容**）。
    """

    code = "draft_section_cross_section_ref"
    message = "draft section output references claim outside this section"


class DraftSectionUnboundEvidence(DraftSectionError):
    """模型输出把 Evidence 与不绑定它的 Claim 关联。

    Evidence 必须真实绑定于段落引用的至少一个 Claim（hard provenance）；
    禁止「引用只属于其他 Claim 的 E」→ 拒绝（0 写）。
    """

    code = "draft_section_unbound_evidence"
    message = "draft section output references evidence not bound to referenced claims"


class DraftSectionNumericGroundingError(DraftSectionError):
    """段落引入的 quantitative token 未逐字出现在所引用 Claim/Evidence 中。

    numeric grounding guard（spec L）：程序从 paragraph.text 提取 quantitative
    tokens，每个必须逐字存在于该段落引用的 Claim statement 或 Evidence
    statement/quote 至少一处；否则拒绝（**不自动改写 / 不二次 LLM**）。
    """

    code = "draft_section_numeric_grounding_error"
    message = "draft section paragraph introduces ungrounded quantitative token"


class DraftSectionForbiddenLanguage(DraftSectionError):
    """段落包含被禁止的投资语言（买入/卖出建议、目标价、收益承诺等）。

    写作文档策略（spec M）：Writer 不得写买卖建议 / 目标价 / 收益承诺 / 保证
    收益；程序确定性检测到即拒绝（0 写）。
    """

    code = "draft_section_forbidden_language"
    message = "draft section paragraph contains forbidden investment language"


class DraftSectionModelUnavailable(DraftSectionError):
    """Draft section writer model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。"""

    code = "draft_section_model_unavailable"
    message = "draft section writer model unavailable"


class DraftSectionPersistenceFailed(DraftSectionError):
    """草稿持久化事务失败（已整条回滚，0 partial write）。"""

    code = "draft_section_persistence_failed"
    message = "draft section persistence failed"
