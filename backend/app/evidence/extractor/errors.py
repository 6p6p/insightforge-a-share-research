"""Evidence extractor error taxonomy (stage 3C.2).

错误消息不包含：完整 chunk 文本、完整 prompt、API key、provider raw response、
DB URL。`code` 是稳定错误码。
"""

from app.evidence.errors import EvidenceError


class EvidenceExtractorError(EvidenceError):
    """证据抽取域稳定错误基类。"""

    code = "evidence_extractor_error"
    message = "evidence extractor error"


class EvidenceExtractorUnavailable(EvidenceExtractorError):
    """Evidence extraction model 不可用（provider 调用失败 / 未配置 / 懒加载缺失）。

    由 adapter 负责把 provider 层失败翻译为本错误；服务不直接依赖具体 provider。
    """

    code = "evidence_extractor_unavailable"
    message = "evidence extractor unavailable"


class EvidenceExtractionMalformedOutput(EvidenceExtractorError):
    """model 返回的结构化输出不符合 EvidenceExtractionDecision schema。

    包括：字段缺失/类型错误/枚举非法、relevant=false 但 items 非空、
    relevant=true 但 items 不在 1..3、reason_code 出现在相关结果上、
    单 response 完全重复 item。
    """

    code = "evidence_extraction_malformed_output"
    message = "evidence extraction malformed output"


class EvidenceExtractionQuoteNotFound(EvidenceExtractorError):
    """LLM 返回的 quote_text 不是 chunk.text 的精确子串（0 次出现）。

    禁止 fuzzy match / normalize 后匹配 / 自动修正标点空白。
    """

    code = "evidence_extraction_quote_not_found"
    message = "evidence extraction quote not found"


class EvidenceExtractionQuoteAmbiguous(EvidenceExtractorError):
    """quote_text 在 chunk.text 中出现 >1 次（含重叠），无法确定所指区间。

    LLM 不返回 char offsets；无法唯一解析时拒绝，而不是猜测。
    """

    code = "evidence_extraction_quote_ambiguous"
    message = "evidence extraction quote ambiguous"


class EvidenceExtractionInputStale(EvidenceExtractorError):
    """RetrievalHit 与当前 PG provenance 不一致（chunk 文本或 5 个 ids 已变）。

    不基于 stale RetrievalHit 创建 Evidence；短 DB read 阶段即抛出。
    """

    code = "evidence_extraction_input_stale"
    message = "evidence extraction input stale"


class EvidenceExtractionInputError(EvidenceExtractorError):
    """调用方输入不合法（如 research_question 为空）。

    区别于 stale：这是调用方直接提供的参数问题，可在调用 LLM 前拒绝。
    """

    code = "evidence_extraction_input_error"
    message = "evidence extraction input error"
