"""Stable error taxonomy for evidence cards (stage 3C.1).

错误消息不包含：chunk 正文、完整 raw content、DB URL、absolute path。
`code` 是稳定错误码，映射自 EvidenceCard 的确定性不变量。
"""


class EvidenceError(Exception):
    """证据域稳定错误基类。"""

    code = "evidence_error"
    message = "evidence error"

    def __init__(self, message: str | None = None) -> None:
        super().__init__(message if message is not None else self.message)


class EvidenceCardDraftError(EvidenceError):
    """EvidenceCardDraft 构造校验失败（语义输入不合法）。

    与 quote / locator / provenance / card integrity 错误区分：这是
    调用方在提供语义输入时就可被拒绝的错误。
    """

    code = "evidence_card_draft_error"
    message = "invalid evidence card draft"


class EvidenceQuoteRangeError(EvidenceError):
    """quote 区间或切片结果不合法（越界 / 空 / 空白）。

    quote_text 由程序从 chunk.text[quote_start:quote_end] 精确切片，
    绝不 normalize / 改写 / 摘要 / 自动纠错。
    """

    code = "evidence_quote_range_error"
    message = "invalid evidence quote range"


class EvidenceLocatorIntegrityError(EvidenceError):
    """locator_refs 无法精确重建 chunk.text（invariant 破坏）或结构非法。

    sum(ref segment lengths) + separators 必须 == len(chunk.text)；
    破坏说明 Chunk locator_refs 与正文不一致，**不自动修复**。
    """

    code = "evidence_locator_integrity_error"
    message = "evidence locator integrity error"


class EvidenceProvenanceIntegrityError(EvidenceError):
    """EvidenceCard 的 provenance 链（Chunk → ChunkSet → ParsedSource →
    SourceRecord → Company）断裂。

    EvidenceCard 只能建立在完整可追溯的证据链上；断裂时不自动修复。
    """

    code = "evidence_provenance_integrity_error"
    message = "evidence provenance integrity error"


class EvidenceCardIntegrityError(EvidenceError):
    """已有 fingerprint 的 EvidenceCard replay 完整性校验失败。

    校验 chunk/source/parsed IDs、quote slice / sha256、locator projection、
    provider、authority tier、critical eligibility、published/reporting
    period、evidence fingerprint 与真实 provenance 一致；任一损坏抛此错误，
    **不自动 repair**（修订 = 新 EvidenceCard）。
    """

    code = "evidence_card_integrity_error"
    message = "evidence card integrity error"


class EvidencePersistenceFailed(EvidenceError):
    """EvidenceCard 持久化事务失败（DB 层错误）。"""

    code = "evidence_persistence_failed"
    message = "evidence card persistence failed"


class EvidenceProviderNotRegisteredError(EvidenceError):
    """user_supplied provider 未在 source_providers 登记（服务端配置问题）。

    user_supplied Evidence 的 authority_tier / critical_claim_eligible 必须
    复制自真实 provider 行；provider 缺失时拒绝登记（不硬编码可信级别）。
    """

    code = "evidence_provider_not_registered"
    message = "user_supplied provider is not registered"
