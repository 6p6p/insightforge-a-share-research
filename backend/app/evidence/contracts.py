"""Evidence card contracts (stage 3C.1): deterministic provenance + quote + locator.

角色边界（Evidence 是 Stage 3 的最小证据单元，Claim 是 Stage 4 分析结论）：
- RetrievalHit = 候选资料（read model，不落库）；
- **EvidenceCard = 已确认与研究问题相关、有明确原文片段和 provenance 的
  原子证据**（本阶段）；不含 supports_claim / contradicts_claim；
- Claim = Stage 4 分析结论（本阶段不创建）。

本模块冻结：
- EVIDENCE_SCHEMA_VERSION = 1；EvidenceType（fact/metric/event/statement/
  context）；EvidenceConfidence（low/medium/high）。
- EvidenceCardDraft：调用方**只能**提供语义输入（research_question /
  evidence_statement / evidence_type / chunk_id / quote_start / quote_end /
  extractor_name / extractor_version / extractor_model_id /
  extractor_confidence）。**不得**提供 company_id / source_id / authority
  tier / provider / published time / locator_refs / quote_text——这些由
  EvidenceCardService 从真实 provenance 确定性派生。
- quote 精确契约：quote_text = chunk.text[quote_start:quote_end] 程序切片，
  不信任调用方 / LLM；quote_text.strip() 非空；越界 → EvidenceQuoteRangeError。
  绝不 normalize / 改写 / 摘要 / 自动纠错。
- locator 投影契约：project_evidence_locator_refs 按 quote 实际覆盖区间
  重建每个 ref 在 chunk 内的 local span 并与 quote 求交，只保留 quote 覆盖
  到的 refs，char_start/end 缩窄到原 ParsedBlock 字符范围，locator 原样保留。
- evidence_fingerprint = canonical JSON + SHA-256（不含 evidence_id /
  created_at）；同一完全相同 Evidence → 同一指纹 → replay 同一卡；
  语义 / quote / extractor version 任一变化 → 新指纹 → 新卡，旧卡保留。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from app.domain.source_records import SourceDocumentType
from app.evidence.errors import (
    EvidenceCardDraftError,
    EvidenceLocatorIntegrityError,
    EvidenceQuoteRangeError,
)

# evidence_cards.evidence_schema_version 的当前值（改名或换结构时递增；
# 已有卡的 evidence_schema_version 原样保留，新语义 → 新 fingerprint）。
# v2 = 泛化 origin 模型（stage 3C.3A）：fingerprint 加入 origin_type；
# 旧 v1 行保留不重算，新卡一律用 v2。
EVIDENCE_SCHEMA_VERSION = 2

_EVIDENCE_TYPES = ("fact", "metric", "event", "statement", "context")
_CONFIDENCE_LEVELS = ("low", "medium", "high")


class EvidenceType(StrEnum):
    """Evidence 的语义分类（不是 Claim，不做正确性判断）。

    - fact: 明确事实描述；
    - metric: 数字 / 指标；
    - event: 已发生事件；
    - statement: 明确陈述（公司/管理层等表态）；
    - context: 研究背景。
    不加 prediction / recommendation / buy / sell / counter_evidence。
    """

    FACT = "fact"
    METRIC = "metric"
    EVENT = "event"
    STATEMENT = "statement"
    CONTEXT = "context"


class EvidenceConfidence(StrEnum):
    """extractor（语义提取器）的置信度，**与来源可靠性无关**。

    authority_tier_snapshot（来源可靠性）≠ extractor_confidence（语义提取
    置信度）。绝不要因 extractor_confidence=high 自动提升
    critical_claim_eligible_snapshot（那直接复制 SourceRecord）。
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class EvidenceOrigin(StrEnum):
    """EvidenceCard 的 origin 模型（stage 3C.3A）：**同一 evidence_card_id
    namespace 下的多 origin，不拆表**。

    - DOCUMENT_CHUNK：经 DocumentChunk → ParsedSource → SourceRecord 链，
      带精确 quote（默认 / 既有 v1 行回填值）；
    - MACRO_OBSERVATION：经 MacroObservation → MacroDatasetSnapshot →
      MacroSeries → SourceProvider → RawArtifact 链，不带 quote；
    - USER_SUPPLIED：用户从官方报告转录（V1.1 final closure）——quote =
      用户粘贴的原文引文（含数字 token，供确定性财务解析），source =
      user_supplied 来源记录；**可信级别 Tier-4 / critical_claim_eligible
      False，绝不伪装成官方自动提取**。

    **不是** Macro → fake DocumentChunk：macro Evidence 不经过
    DocumentChunk / ParsedSource / Chroma / quote resolver。
    """

    DOCUMENT_CHUNK = "document_chunk"
    MACRO_OBSERVATION = "macro_observation"
    USER_SUPPLIED = "user_supplied"
    # 自动财务提取（deterministic，0 LLM）：quote = ParsedSourceBlock 文本的
    # 逐字切片（含精确数字 token），source = 原始报告 SourceRecord（tier 快照
    # 继承报告来源）。与 user_supplied 一样不经过 Chroma / chunk quote resolver。
    FINANCIAL_EXTRACTION = "financial_extraction"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_research_question_sha256(research_question: str) -> str:
    """research_question（trim 后）UTF-8 文本的 SHA-256。

    research_question 不新建表：trim 后保留原文本，sha256 只用于索引与
    指纹；本阶段不伪造 question UUID。
    """
    return _sha256_hex(research_question.strip())


def compute_quote_sha256(quote_text: str) -> str:
    """quote_text（精确切片原文）UTF-8 文本的 SHA-256。"""
    return _sha256_hex(quote_text)


def derive_quote_text(*, chunk_text: str, quote_start: int, quote_end: int) -> str:
    """quote 精确切片契约：quote_text = chunk.text[quote_start:quote_end]。

    由程序生成，不信任调用方 / LLM 提供的 quote 文本：
    - 越界 / 区间非法 → EvidenceQuoteRangeError；
    - quote_text.strip() 为空 → EvidenceQuoteRangeError（quote 不能只含空白）；
    - 绝不 normalize / 改写 / 摘要 / 自动纠错（quote 是原文切片）。
    """
    if not isinstance(chunk_text, str):
        raise EvidenceQuoteRangeError("chunk_text 必须是 str")
    if (
        isinstance(quote_start, bool)
        or isinstance(quote_end, bool)
        or not isinstance(quote_start, int)
        or not isinstance(quote_end, int)
    ):
        raise EvidenceQuoteRangeError("quote_start/quote_end 必须是 int")
    if quote_start < 0 or quote_end <= quote_start:
        raise EvidenceQuoteRangeError("quote 区间非法：quote_start >= 0 且 quote_end > quote_start")
    if quote_start > len(chunk_text) or quote_end > len(chunk_text):
        raise EvidenceQuoteRangeError("quote 区间超出 chunk 文本范围")
    quote_text = chunk_text[quote_start:quote_end]
    if not quote_text.strip():
        raise EvidenceQuoteRangeError("quote_text trim 后不能为空")
    return quote_text


def project_evidence_locator_refs(
    chunk_text: str,
    locator_refs: list[dict],
    quote_start: int,
    quote_end: int,
) -> list[dict]:
    """把整 chunk 的 locator_refs 投影为 **quote 级** EvidenceCard locator_refs。

    chunk.text 由每个 ref 对应的原 block slice（长度 = char_end - char_start）
    以 "\\n" 连接。算法：
    1. 校验 invariant：sum(ref segment lengths) + (n-1) separators ==
       len(chunk.text)，不一致 → EvidenceLocatorIntegrityError；
    2. 按 ref 顺序重建每个 ref 在 chunk 内的 local span；
    3. 与 quote [start, end) 求交：只保留 quote 实际覆盖到的 refs，char_start/
       end 缩窄到原 ParsedBlock 对应字符范围；
    4. locator 原样保留（HTML: xpath/element_id；PDF: page_number/bbox）。

    EvidenceCard.locator_refs 必须是 quote 级，不能简单复制整个 chunk 的
    locator_refs。
    """
    if not isinstance(locator_refs, list):
        raise EvidenceLocatorIntegrityError("locator_refs 必须是 list")
    refs = list(locator_refs)
    total_segment = 0
    for ref in refs:
        if not isinstance(ref, dict):
            raise EvidenceLocatorIntegrityError("locator_ref 必须是 dict")
        ordinal = ref.get("block_ordinal")
        start = ref.get("char_start")
        end = ref.get("char_end")
        locator = ref.get("locator")
        if isinstance(ordinal, bool) or not isinstance(ordinal, int) or ordinal < 1:
            raise EvidenceLocatorIntegrityError("block_ordinal 必须是 >= 1 的 int")
        if isinstance(start, bool) or not isinstance(start, int) or start < 0:
            raise EvidenceLocatorIntegrityError("char_start 必须是 >= 0 的 int")
        if isinstance(end, bool) or not isinstance(end, int) or end <= start:
            raise EvidenceLocatorIntegrityError("char_end 必须 > char_start")
        if not isinstance(locator, dict):
            raise EvidenceLocatorIntegrityError("locator 必须是 dict")
        total_segment += end - start
    if total_segment + (len(refs) - 1) != len(chunk_text):
        raise EvidenceLocatorIntegrityError(
            "chunk locator_refs 无法精确重建 chunk.text（sum + separators 不一致）"
        )

    projected: list[dict] = []
    local_pos = 0
    for ref in refs:
        segment_len = ref["char_end"] - ref["char_start"]
        ref_local_start = local_pos
        ref_local_end = local_pos + segment_len
        local_pos = ref_local_end + 1  # "\n" separator
        overlap_start = max(ref_local_start, quote_start)
        overlap_end = min(ref_local_end, quote_end)
        if overlap_end <= overlap_start:
            continue
        projected.append(
            {
                "block_ordinal": ref["block_ordinal"],
                "char_start": ref["char_start"] + (overlap_start - ref_local_start),
                "char_end": ref["char_start"] + (overlap_end - ref_local_start),
                "locator": ref["locator"],
            }
        )
    return projected


@dataclass(frozen=True)
class EvidenceCardDraft:
    """调用方提交的证据语义输入（构造时校验，不可变）。

    只允许提供语义输入；company_id / source_id / authority tier / provider /
    published time / locator_refs / quote_text 一律由 Service 从真实
    provenance 确定性派生，调用方**不得**提供。

    - research_question / evidence_statement：trim 后非空，构造时归一化为
      trim 值（保留原文本，不伪造 question UUID）；
    - quote_start / quote_end：chunk.text 的 Python [start, end) 字符区间
      （quote_text 由 Service 程序切片）；
    - extractor_name / extractor_version / extractor_confidence：语义提取器
      身份与置信度；extractor_model_id 可选（提供时 trim，空串归一化为 None）。
    """

    research_question: str
    evidence_statement: str
    evidence_type: EvidenceType
    chunk_id: UUID
    quote_start: int
    quote_end: int
    extractor_name: str
    extractor_version: int
    extractor_model_id: str | None = None
    extractor_confidence: EvidenceConfidence = EvidenceConfidence.MEDIUM

    def __post_init__(self) -> None:
        question = self.research_question.strip()
        if not question:
            raise EvidenceCardDraftError("research_question 不能为空（trim 后）")
        statement = self.evidence_statement.strip()
        if not statement:
            raise EvidenceCardDraftError("evidence_statement 不能为空（trim 后）")
        if not isinstance(self.evidence_type, EvidenceType):
            raise EvidenceCardDraftError("evidence_type 必须是 EvidenceType")
        if isinstance(self.chunk_id, bool) or not isinstance(self.chunk_id, UUID):
            raise EvidenceCardDraftError("chunk_id 必须是 UUID")
        if (
            isinstance(self.quote_start, bool)
            or isinstance(self.quote_end, bool)
            or not isinstance(self.quote_start, int)
            or not isinstance(self.quote_end, int)
        ):
            raise EvidenceCardDraftError("quote_start/quote_end 必须是 int")
        if self.quote_start < 0:
            raise EvidenceCardDraftError("quote_start 必须 >= 0")
        if self.quote_end <= self.quote_start:
            raise EvidenceCardDraftError("quote_end 必须 > quote_start")
        name = self.extractor_name.strip()
        if not name:
            raise EvidenceCardDraftError("extractor_name 不能为空（trim 后）")
        if (
            isinstance(self.extractor_version, bool)
            or not isinstance(self.extractor_version, int)
            or self.extractor_version < 1
        ):
            raise EvidenceCardDraftError("extractor_version 必须 >= 1")
        if not isinstance(self.extractor_confidence, EvidenceConfidence):
            raise EvidenceCardDraftError("extractor_confidence 必须是 EvidenceConfidence")
        model_id = self.extractor_model_id
        if model_id is not None:
            model_id = model_id.strip()
            if not model_id:
                model_id = None
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "evidence_statement", statement)
        object.__setattr__(self, "extractor_name", name)
        object.__setattr__(self, "extractor_model_id", model_id)


def compute_evidence_fingerprint(
    *,
    evidence_schema_version: int,
    origin_type: str,
    company_id: UUID,
    source_id: UUID,
    parsed_source_id: UUID,
    chunk_set_id: UUID,
    chunk_id: UUID,
    research_question: str,
    evidence_statement: str,
    evidence_type: str,
    quote_start: int,
    quote_end: int,
    quote_sha256: str,
    locator_refs: list[dict],
    provider_key: str,
    source_published_at: datetime | None,
    reporting_period_end: date | None,
    authority_tier_snapshot: int,
    critical_claim_eligible_snapshot: bool,
    extractor_name: str,
    extractor_version: int,
    extractor_model_id: str | None,
    extractor_confidence: str,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：evidence_schema_version、origin_type、company/source/
    parsed_source/chunk_set/chunk ids、research_question/evidence_statement/
    evidence_type、quote_start/quote_end/quote_sha256/locator_refs、
    provider_key/authority_tier_snapshot/critical_claim_eligible_snapshot/
    source_published_at/reporting_period_end、extractor_name/version/
    model_id/confidence。

    **不得包含** evidence_id / created_at。同一完全相同 Evidence → 同一
    指纹 → replay 同一卡；语义 / quote / extractor version 任一变化 →
    新指纹 → 新卡，旧卡保留（修订 = 新 EvidenceCard）。v2 起加入
    origin_type（document path 固定为 'document_chunk'）；macro path 用
    compute_macro_evidence_fingerprint。
    """
    payload = {
        "evidence_schema_version": evidence_schema_version,
        "origin_type": origin_type,
        "company_id": str(company_id),
        "source_id": str(source_id),
        "parsed_source_id": str(parsed_source_id),
        "chunk_set_id": str(chunk_set_id),
        "chunk_id": str(chunk_id),
        "research_question": research_question,
        "evidence_statement": evidence_statement,
        "evidence_type": evidence_type,
        "quote_start": quote_start,
        "quote_end": quote_end,
        "quote_sha256": quote_sha256,
        "locator_refs": locator_refs,
        "provider_key": provider_key,
        "authority_tier_snapshot": authority_tier_snapshot,
        "critical_claim_eligible_snapshot": critical_claim_eligible_snapshot,
        "source_published_at": (
            source_published_at.isoformat() if source_published_at is not None else None
        ),
        "reporting_period_end": (
            reporting_period_end.isoformat() if reporting_period_end is not None else None
        ),
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "extractor_model_id": extractor_model_id,
        "extractor_confidence": extractor_confidence,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class MacroEvidenceDraft:
    """macro Evidence 的语义输入（构造时校验，不可变；stage 3C.3A）。

    只允许提供：company_id（当前研究公司，由调用方上下文提供）、
    research_question、macro_observation_id、evidence_statement 与 extractor
    身份。**不得**提供 value / period / provider / snapshot / series /
    locator / authority tier——这些由 MacroEvidenceService 从真实 Macro
    provenance 确定性派生。

    - evidence_type 固定为 metric（无字段），调用方不可指定；
    - quote 字段固定为 NULL（macro 无原文 quote，不经过 quote resolver）。
    """

    company_id: UUID
    research_question: str
    macro_observation_id: UUID
    evidence_statement: str
    extractor_name: str
    extractor_version: int
    extractor_model_id: str | None = None
    extractor_confidence: EvidenceConfidence = EvidenceConfidence.MEDIUM

    def __post_init__(self) -> None:
        question = self.research_question.strip()
        if not question:
            raise EvidenceCardDraftError("research_question 不能为空（trim 后）")
        statement = self.evidence_statement.strip()
        if not statement:
            raise EvidenceCardDraftError("evidence_statement 不能为空（trim 后）")
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise EvidenceCardDraftError("company_id 必须是 UUID")
        if isinstance(self.macro_observation_id, bool) or not isinstance(
            self.macro_observation_id, UUID
        ):
            raise EvidenceCardDraftError("macro_observation_id 必须是 UUID")
        name = self.extractor_name.strip()
        if not name:
            raise EvidenceCardDraftError("extractor_name 不能为空（trim 后）")
        if (
            isinstance(self.extractor_version, bool)
            or not isinstance(self.extractor_version, int)
            or self.extractor_version < 1
        ):
            raise EvidenceCardDraftError("extractor_version 必须 >= 1")
        if not isinstance(self.extractor_confidence, EvidenceConfidence):
            raise EvidenceCardDraftError("extractor_confidence 必须是 EvidenceConfidence")
        model_id = self.extractor_model_id
        if model_id is not None:
            model_id = model_id.strip()
            if not model_id:
                model_id = None
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "evidence_statement", statement)
        object.__setattr__(self, "extractor_name", name)
        object.__setattr__(self, "extractor_model_id", model_id)


def build_macro_observation_locator(
    *,
    provider_key: str,
    series_id: UUID,
    snapshot_id: UUID,
    observation_id: UUID,
    source_id: str,
    external_indicator_id: str,
    geography_code: str,
    frequency: str,
    period: str,
    normalized_period_start: date,
) -> list[dict]:
    """macro EvidenceCard 的 deterministic structured locator（单元素 array）。

    locator_refs 契约（CK jsonb_array_length > 0）：macro 保存结构化的
    provenance 定位器（类型 + provider/series/snapshot/observation identity +
    period），**不造 fake 文本 / 不经过 Chroma / quote resolver**。只使用
    Macro models 已有的真实字段。
    """
    return [
        {
            "type": "macro_observation",
            "provider_key": provider_key,
            "series_id": str(series_id),
            "snapshot_id": str(snapshot_id),
            "observation_id": str(observation_id),
            "source_id": source_id,
            "external_indicator_id": external_indicator_id,
            "geography_code": geography_code,
            "frequency": frequency,
            "period": period,
            "normalized_period_start": normalized_period_start.isoformat(),
        }
    ]


def compute_macro_evidence_fingerprint(
    *,
    evidence_schema_version: int,
    origin_type: str,
    company_id: UUID,
    research_question: str,
    evidence_statement: str,
    evidence_type: str,
    macro_observation_id: UUID,
    macro_snapshot_id: UUID,
    macro_series_id: UUID,
    period: str,
    normalized_period_start: date,
    value_numeric: Decimal | None,
    is_missing: bool,
    provider_key: str,
    authority_tier_snapshot: int,
    critical_claim_eligible_snapshot: bool,
    locator_refs: list[dict],
    extractor_name: str,
    extractor_version: int,
    extractor_model_id: str | None,
    extractor_confidence: str,
) -> str:
    """macro Evidence 的确定性 SHA-256 指纹（sort_keys + 固定 separators）。

    至少覆盖：evidence_schema_version、origin_type、company_id、
    research_question、macro identity（observation/snapshot/series + period
    + normalized_period_start + value + is_missing）、evidence_statement /
    evidence_type（固定 metric）、provider_key / authority_tier_snapshot /
    critical_claim_eligible_snapshot（来自 Macro provenance，不硬编码）、
    locator_refs、extractor_name/version/model_id/confidence。

    **不得包含** evidence_id / created_at。同一完全相同 macro Evidence →
    同一指纹 → replay 同一卡；statement / extractor version / 上游 snapshot
    任一变化 → 新指纹 → 新卡，旧卡保留。value 用 Decimal → str 序列化，
    避免浮点 / 精度歧义。
    """
    payload = {
        "evidence_schema_version": evidence_schema_version,
        "origin_type": origin_type,
        "company_id": str(company_id),
        "research_question": research_question,
        "evidence_statement": evidence_statement,
        "evidence_type": evidence_type,
        "macro_observation_id": str(macro_observation_id),
        "macro_snapshot_id": str(macro_snapshot_id),
        "macro_series_id": str(macro_series_id),
        "period": period,
        "normalized_period_start": normalized_period_start.isoformat(),
        "value_numeric": str(value_numeric) if value_numeric is not None else None,
        "is_missing": is_missing,
        "provider_key": provider_key,
        "authority_tier_snapshot": authority_tier_snapshot,
        "critical_claim_eligible_snapshot": critical_claim_eligible_snapshot,
        "locator_refs": locator_refs,
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "extractor_model_id": extractor_model_id,
        "extractor_confidence": extractor_confidence,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class UserSuppliedEvidenceDraft:
    """USER_SUPPLIED Evidence 的语义输入（构造时校验，不可变；V1.1 closure）。

    用户从官方报告 / 官网公告**人工转录**的 Evidence：
    - company_id / research_question：当前研究上下文（调用方提供）；
    - evidence_statement / evidence_type：用户陈述与类型；
    - quote_text：用户粘贴的**原文引文**（trim 后非空；财务观察路径要求
      引文包含精确数字 token，供确定性解析，见 FinancialMetricService）；
    - source_title：来源名称（如“2023年年度报告”），用于创建 user_supplied
      SourceRecord；
    - source_url：官方来源 URL（可选；source_url 已允许 NULL）；
    - source_published_at / reporting_period_end：来源发布/报告期（可选）。

    **可信级别语义**：extractor 身份固定为 user_transcription v1、
    confidence=low —— 用户转录未经任何自动提取校验；authority_tier_snapshot
    由服务从 user_supplied provider（Tier-4）复制，critical_claim_eligible
    = False。绝不伪装成官方自动提取。
    """

    company_id: UUID
    research_question: str
    evidence_statement: str
    evidence_type: EvidenceType
    quote_text: str
    source_title: str
    document_type: SourceDocumentType = SourceDocumentType.OTHER
    source_url: str | None = None
    source_published_at: datetime | None = None
    reporting_period_end: date | None = None

    def __post_init__(self) -> None:
        question = self.research_question.strip()
        if not question:
            raise EvidenceCardDraftError("research_question 不能为空（trim 后）")
        statement = self.evidence_statement.strip()
        if not statement:
            raise EvidenceCardDraftError("evidence_statement 不能为空（trim 后）")
        quote = self.quote_text.strip()
        if not quote:
            raise EvidenceCardDraftError("quote_text 不能为空（trim 后）")
        title = self.source_title.strip()
        if not title:
            raise EvidenceCardDraftError("source_title 不能为空（trim 后）")
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise EvidenceCardDraftError("company_id 必须是 UUID")
        if not isinstance(self.evidence_type, EvidenceType):
            raise EvidenceCardDraftError("evidence_type 必须是 EvidenceType")
        if not isinstance(self.document_type, SourceDocumentType):
            raise EvidenceCardDraftError("document_type 必须是 SourceDocumentType")
        url = self.source_url
        if url is not None:
            url = url.strip()
            if not url:
                url = None
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "evidence_statement", statement)
        object.__setattr__(self, "quote_text", quote)
        object.__setattr__(self, "source_title", title)
        object.__setattr__(self, "source_url", url)


def build_user_supplied_locator(*, source_id: UUID, source_url: str | None) -> list[dict]:
    """user_supplied EvidenceCard 的 deterministic structured locator。

    locator_refs 契约：user_supplied origin 允许空数组，但这里保存结构化
    provenance 定位器（类型 + source identity + 用户提供的官方 URL），
    **不造 fake 文本 / 不经过 Chroma**。
    """
    return [
        {
            "type": "user_supplied",
            "source_id": str(source_id),
            "source_url": source_url,
        }
    ]


def compute_user_supplied_evidence_fingerprint(
    *,
    evidence_schema_version: int,
    origin_type: str,
    company_id: UUID,
    source_id: UUID,
    research_question: str,
    evidence_statement: str,
    evidence_type: str,
    quote_text: str,
    quote_sha256: str,
    locator_refs: list[dict],
    provider_key: str,
    authority_tier_snapshot: int,
    critical_claim_eligible_snapshot: bool,
    source_url: str | None,
    source_published_at: datetime | None,
    reporting_period_end: date | None,
    extractor_name: str,
    extractor_version: int,
    extractor_model_id: str | None,
    extractor_confidence: str,
) -> str:
    """USER_SUPPLIED Evidence 的确定性 SHA-256 指纹（sort_keys + 固定 separators）。

    至少覆盖：evidence_schema_version、origin_type、company/source ids、
    research_question/evidence_statement/evidence_type、quote_text（原文，
    不 normalize）/quote_sha256/locator_refs、provider_key/
    authority_tier_snapshot/critical_claim_eligible_snapshot、source_url/
    source_published_at/reporting_period_end、extractor 身份。

    **不得包含** evidence_id / created_at。同一完全相同 user_supplied
    Evidence → 同一指纹 → replay 同一卡；引文 / 陈述 / URL 任一变化 →
    新指纹 → 新卡，旧卡保留。
    """
    payload = {
        "evidence_schema_version": evidence_schema_version,
        "origin_type": origin_type,
        "company_id": str(company_id),
        "source_id": str(source_id),
        "research_question": research_question,
        "evidence_statement": evidence_statement,
        "evidence_type": evidence_type,
        "quote_text": quote_text,
        "quote_sha256": quote_sha256,
        "locator_refs": locator_refs,
        "provider_key": provider_key,
        "authority_tier_snapshot": authority_tier_snapshot,
        "critical_claim_eligible_snapshot": critical_claim_eligible_snapshot,
        "source_url": source_url,
        "source_published_at": (
            source_published_at.isoformat() if source_published_at is not None else None
        ),
        "reporting_period_end": (
            reporting_period_end.isoformat() if reporting_period_end is not None else None
        ),
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "extractor_model_id": extractor_model_id,
        "extractor_confidence": extractor_confidence,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class FinancialExtractionEvidenceDraft:
    """FINANCIAL_EXTRACTION Evidence 的语义输入（Final Autonomous Research）。

    自动财务提取（deterministic，0 LLM）产生的证据：
    - company_id / research_question：研究上下文（调用方提供）；
    - source_id：**原始报告 SourceRecord**（tier / critical 快照继承报告来源，
      不硬编码）；
    - parsed_source_id / quote_block_id / quote_start / quote_end：quote 在
      ParsedSourceBlock 文本中的精确切片（逐字）；
    - quote_text / evidence_statement / evidence_type（固定 metric）。

    extractor 身份固定为 financial_extraction v1 / low（确定性提取，非 LLM）。
    """

    company_id: UUID
    research_question: str
    source_id: UUID
    parsed_source_id: UUID
    quote_block_id: UUID
    quote_start: int
    quote_end: int
    quote_text: str
    evidence_statement: str
    evidence_type: EvidenceType = EvidenceType.METRIC

    def __post_init__(self) -> None:
        question = self.research_question.strip()
        if not question:
            raise EvidenceCardDraftError("research_question 不能为空（trim 后）")
        statement = self.evidence_statement.strip()
        if not statement:
            raise EvidenceCardDraftError("evidence_statement 不能为空（trim 后）")
        quote = self.quote_text.strip()
        if not quote:
            raise EvidenceCardDraftError("quote_text 不能为空（trim 后）")
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise EvidenceCardDraftError("company_id 必须是 UUID")
        if isinstance(self.source_id, bool) or not isinstance(self.source_id, UUID):
            raise EvidenceCardDraftError("source_id 必须是 UUID")
        if isinstance(self.parsed_source_id, bool) or not isinstance(self.parsed_source_id, UUID):
            raise EvidenceCardDraftError("parsed_source_id 必须是 UUID")
        if isinstance(self.quote_block_id, bool) or not isinstance(self.quote_block_id, UUID):
            raise EvidenceCardDraftError("quote_block_id 必须是 UUID")
        if not isinstance(self.evidence_type, EvidenceType):
            raise EvidenceCardDraftError("evidence_type 必须是 EvidenceType")
        if isinstance(self.quote_start, bool) or not isinstance(self.quote_start, int):
            raise EvidenceCardDraftError("quote_start 必须是 int")
        if isinstance(self.quote_end, bool) or not isinstance(self.quote_end, int):
            raise EvidenceCardDraftError("quote_end 必须是 int")
        if self.quote_start < 0 or self.quote_end <= self.quote_start:
            raise EvidenceCardDraftError("quote_start/quote_end 区间非法")
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "evidence_statement", statement)
        object.__setattr__(self, "quote_text", quote)


def build_financial_extraction_locator(
    *,
    source_id: UUID,
    parsed_source_id: UUID,
    block_id: UUID,
    page_number: int | None = None,
    line_index: int | None = None,
) -> list[dict]:
    """FINANCIAL_EXTRACTION EvidenceCard 的 deterministic structured locator。

    locator_refs 契约（单元素 array）：保存结构化 provenance 定位器（block
    身份 + page/line），**不经过 Chroma / quote resolver**。
    """
    return [
        {
            "type": "financial_extraction",
            "source_id": str(source_id),
            "parsed_source_id": str(parsed_source_id),
            "block_id": str(block_id),
            "page_number": page_number,
            "line_index": line_index,
        }
    ]


def compute_financial_extraction_evidence_fingerprint(
    *,
    evidence_schema_version: int,
    origin_type: str,
    company_id: UUID,
    source_id: UUID,
    parsed_source_id: UUID,
    quote_block_id: UUID,
    research_question: str,
    evidence_statement: str,
    evidence_type: str,
    quote_text: str,
    quote_sha256: str,
    quote_start: int,
    quote_end: int,
    locator_refs: list[dict],
    provider_key: str,
    authority_tier_snapshot: int,
    critical_claim_eligible_snapshot: bool,
    reporting_period_end: date | None,
    extractor_name: str,
    extractor_version: int,
    extractor_model_id: str | None,
    extractor_confidence: str,
) -> str:
    """FINANCIAL_EXTRACTION Evidence 的确定性 SHA-256 指纹。

    至少覆盖：schema version / origin_type / company / source / parsed_source /
    block ids / research_question / evidence_statement / evidence_type /
    quote（原文 + sha256 + start/end）/ locator_refs / provider_key /
    authority tier / critical 快照 / reporting_period_end / extractor 身份。

    **不得包含** evidence_id / created_at。同一完全相同 Evidence → replay
    同一卡；quote / 陈述 / 上游报告任一变化 → 新指纹 → 新卡，旧卡保留。
    """
    payload = {
        "evidence_schema_version": evidence_schema_version,
        "origin_type": origin_type,
        "company_id": str(company_id),
        "source_id": str(source_id),
        "parsed_source_id": str(parsed_source_id),
        "quote_block_id": str(quote_block_id),
        "research_question": research_question,
        "evidence_statement": evidence_statement,
        "evidence_type": evidence_type,
        "quote_text": quote_text,
        "quote_sha256": quote_sha256,
        "quote_start": quote_start,
        "quote_end": quote_end,
        "locator_refs": locator_refs,
        "provider_key": provider_key,
        "authority_tier_snapshot": authority_tier_snapshot,
        "critical_claim_eligible_snapshot": critical_claim_eligible_snapshot,
        "reporting_period_end": (
            reporting_period_end.isoformat() if reporting_period_end is not None else None
        ),
        "extractor_name": extractor_name,
        "extractor_version": extractor_version,
        "extractor_model_id": extractor_model_id,
        "extractor_confidence": extractor_confidence,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
