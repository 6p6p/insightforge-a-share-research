"""Evidence extractor contracts (stage 3C.2): structured output + LLM Protocol.

角色边界（Extractor 只做语义，确定性交给代码）：
- Extractor 负责：判断 RetrievalHit 与研究问题相关性、提取被原文直接支持的
  原子 evidence_statement、选择 evidence_type、选择 low/medium/high
  extractor_confidence、返回逐字 quote_text。
- Extractor **不负责**：quote_start/end、locator、provenance IDs、authority
  tier、critical eligibility、evidence fingerprint、Claim、投资建议——这些
  继续由确定性代码（EvidenceCardService + 3C.1）负责。
- Extractor **不调用** RetrievalService：输入是已经得到的 RetrievalHit。

冻结：
- EVIDENCE_EXTRACTOR_NAME = "structured_llm"；EVIDENCE_EXTRACTOR_VERSION = 1。
  version 代表：prompt contract + structured schema + quote extraction
  semantics；任一行为改变必须 bump（不用新增 migration）。
- EvidenceExtractionItem / EvidenceExtractionDecision（Pydantic 结构化输出）：
  relevant=false → items 必须为空；relevant=true → 1..3 items；单 response
  不允许完全重复 item。**无 reasoning / chain_of_thought / free-form analysis
  字段**。
- EvidenceExtractionModel（Protocol）：LLM abstraction。domain/service 不直接
  依赖具体 DeepSeek/OpenAI provider；自动测试一律用 FakeEvidenceExtractionModel。
  `model_id` 是持久化用的稳定 identifier（provider:model@revision 或明确
  model id，绝不伪造 revision）。
"""

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from app.evidence.contracts import EvidenceConfidence, EvidenceType
from app.rag.retrieval.contracts import RetrievalHit

EVIDENCE_EXTRACTOR_NAME = "structured_llm"
EVIDENCE_EXTRACTOR_VERSION = 1

# 单 RetrievalHit 最多返回 3 个 Evidence item（→ 最多 3 卡）。
MAX_EXTRACTION_ITEMS_PER_HIT = 3


class EvidenceExtractionReason(StrEnum):
    """reason_code：仅用于非相关 / 无证据（relevant=false），可选。

    不允许出现 prediction / recommendation / buy / sell。
    """

    NOT_RELEVANT = "not_relevant"
    INSUFFICIENT_DIRECT_SUPPORT = "insufficient_direct_support"
    AMBIGUOUS_SOURCE_CONTEXT = "ambiguous_source_context"


class EvidenceExtractionItem(BaseModel):
    """单个原子证据（Pydantic 结构化输出）。

    - evidence_statement / quote_text：trim 后非空（**不自动 strip 存储值**，
      quote_text 保持逐字原文，由确定性 resolve_exact_quote 精确匹配）；
    - quote_text 必须逐字复制 chunk.text；LLM **不返回** char offsets；
    - 无 reasoning / chain_of_thought / free-form analysis 字段。
    """

    model_config = ConfigDict(frozen=True)

    evidence_statement: str
    evidence_type: EvidenceType
    quote_text: str
    confidence: EvidenceConfidence

    @model_validator(mode="after")
    def _validate_blank(self) -> "EvidenceExtractionItem":
        if not self.evidence_statement.strip():
            raise ValueError("evidence_statement 不能为空（trim 后）")
        if not self.quote_text.strip():
            raise ValueError("quote_text 不能为空（trim 后）")
        return self


class EvidenceExtractionDecision(BaseModel):
    """一次抽取的结构化决策。

    规则（Pydantic 构造时强制，违反 → ValidationError → 服务层翻译为
    EvidenceExtractionMalformedOutput）：
    - relevant=false → items 必须为空；reason_code 可选（仅限非相关/无证据）；
    - relevant=true → items 必须 1..3 个；reason_code 必须为 None；
    - 单 response 不允许完全重复 item（statement/type/quote/confidence 全同）。
    """

    model_config = ConfigDict(frozen=True)

    relevant: bool
    items: list[EvidenceExtractionItem]
    reason_code: EvidenceExtractionReason | None = None

    @model_validator(mode="after")
    def _validate_rules(self) -> "EvidenceExtractionDecision":
        if not self.relevant and self.items:
            raise ValueError("relevant=false 时 items 必须为空")
        if self.relevant and not (1 <= len(self.items) <= MAX_EXTRACTION_ITEMS_PER_HIT):
            raise ValueError(f"relevant=true 时 items 必须在 1..{MAX_EXTRACTION_ITEMS_PER_HIT}")
        if self.relevant and self.reason_code is not None:
            raise ValueError("reason_code 仅用于非相关/无证据")
        seen: set[tuple] = set()
        for item in self.items:
            key = (
                item.evidence_statement,
                item.evidence_type,
                item.quote_text,
                item.confidence,
            )
            if key in seen:
                raise ValueError("单 response 不允许完全重复 item")
            seen.add(key)
        return self


@runtime_checkable
class EvidenceExtractionModel(Protocol):
    """LLM abstraction：把研究问题 + RetrievalHit 抽成结构化决策。

    - `model_id`：稳定 identifier（provider:model@revision 或明确 model id，
      不伪造 revision）；由 EvidenceCardService 持久化到
      EvidenceCard.extractor_model_id。
    - `extract`：接收 research_question（trim 后非空）与 RetrievalHit，
      返回 EvidenceExtractionDecision；provider 失败翻译为
      EvidenceExtractorUnavailable。
    - 实现不得启用 tools / web search / function side effects。
    """

    model_id: str

    async def extract(
        self,
        research_question: str,
        retrieval_hit: RetrievalHit,
    ) -> EvidenceExtractionDecision: ...
