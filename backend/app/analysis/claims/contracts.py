"""Structured claim analysis contracts (stage 4B.1): request + Evidence Pack + LLM Protocol.

4B.1 建立首个 Structured Analyst 基础设施，只支持 business / event / risk 三个
analysis domain（financial / macro / valuation → ClaimAnalysisDomainNotReady）。

角色边界（Analyst 只做判断，确定性交给代码）：
- Analyst 负责：判断证据与研究问题相关性、生成结构化 ClaimCandidate
  （statement / claim_kind / confidence / importance / E 编号引用）；
- 确定性代码负责：Evidence Pack 构造（E1..En 局部 alias，按 str(evidence_card_id)
  排序）、ref resolution（E → evidence_card_id）、未知引用 / 跨 relation 冲突拒绝、
  domain ↔ claim_kind 兼容性、ClaimService.create_claim_batch 原子持久化；
- Analyst **不负责**：Retrieval / 搜索 / 访问 Chroma / 读 RawArtifact / 写数据库。

冻结：
- CLAIM_ANALYST_NAME = "structured_claim_analyst"；CLAIM_ANALYST_VERSION = 1；
  MAX_EVIDENCE_PER_REQUEST = 30；MAX_CLAIMS_PER_DECISION = 5。
- ClaimCandidate：claim_kind 只允许 fact / inference / risk（**不输出
  relative_valuation**）；每条 Claim ≥1 support_ref；所有 ref 格式 E<number>。
- ClaimAnalysisDecision：relevant=false → claims 必须为空；relevant=true →
  1..5 claims 且 reason_code 为 None。**无 reasoning / chain_of_thought /
  free-form / analysis_domain / company_id / evidence UUID / provider policy 字段**。
- ClaimAnalysisModel（Protocol）：LLM abstraction。domain 不直接依赖具体 provider；
  自动测试一律用 FakeClaimAnalysisModel。
"""

import re
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.analysis.claims.errors import ClaimAnalysisDomainNotReady, ClaimAnalysisInputError
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)

# structured claim analyst 的身份常量（persisted analyst_name = 具体 strategy）。
CLAIM_ANALYST_NAME = "structured_claim_analyst"
CLAIM_ANALYST_VERSION = 1

# 单次分析最多 30 条 Evidence（Evidence Pack 上限）。
MAX_EVIDENCE_PER_REQUEST = 30
# 单次决策最多 5 条 ClaimCandidate（→ create_claim_batch 上限）。
MAX_CLAIMS_PER_DECISION = 5

# 4B.1 支持的分析领域（其余 → ClaimAnalysisDomainNotReady）。
_SUPPORTED_DOMAINS_4B1 = frozenset(
    {ClaimAnalysisDomain.BUSINESS, ClaimAnalysisDomain.EVENT, ClaimAnalysisDomain.RISK}
)

# ClaimCandidate 允许的 claim_kind（4B.1 不输出 relative_valuation）。
_ALLOWED_KINDS_4B1 = frozenset({ClaimKind.FACT, ClaimKind.INFERENCE, ClaimKind.RISK})

_EVIDENCE_REF_PATTERN = re.compile(r"^E\d+$")


class ClaimAnalysisReason(StrEnum):
    """reason_code：仅用于非相关 / 无证据（relevant=false），可选。

    不允许出现 prediction / recommendation / buy / sell。
    """

    NOT_RELEVANT = "not_relevant"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class ClaimCandidate(BaseModel):
    """单条 Claim 候选（Pydantic 结构化输出，模型生成）。

    - statement：trim 后非空；
    - claim_kind：只允许 fact / inference / risk（schema 层拒绝 relative_valuation）；
    - support_refs / contradict_refs / context_refs：全部 E<number> 格式；
    - 每条 Claim ≥1 support_ref；
    - **无 reasoning / chain_of_thought / free-form / analysis_domain /
      company_id / evidence UUID / provider policy 字段**——analysis_domain 由
      request 决定，不是 LLM 决定。
    """

    model_config = ConfigDict(frozen=True)

    statement: str
    claim_kind: ClaimKind
    confidence: ClaimConfidence
    importance: ClaimImportance
    support_refs: list[str]
    contradict_refs: list[str]
    context_refs: list[str]

    @model_validator(mode="after")
    def _validate(self) -> "ClaimCandidate":
        if not self.statement.strip():
            raise ValueError("statement 不能为空（trim 后）")
        for refs in (self.support_refs, self.contradict_refs, self.context_refs):
            for ref in refs:
                if not _EVIDENCE_REF_PATTERN.fullmatch(ref):
                    raise ValueError(f"evidence ref 必须是 E<number> 格式: {ref!r}")
        if not self.support_refs:
            raise ValueError("每条 Claim 至少 1 个 support_ref")
        if self.claim_kind == ClaimKind.RELATIVE_VALUATION:
            raise ValueError("4B.1 不输出 relative_valuation")
        if len(self.support_refs) != len(set(self.support_refs)):
            raise ValueError("support_refs 内不允许重复")
        if len(self.contradict_refs) != len(set(self.contradict_refs)):
            raise ValueError("contradict_refs 内不允许重复")
        if len(self.context_refs) != len(set(self.context_refs)):
            raise ValueError("context_refs 内不允许重复")
        return self


class ClaimAnalysisDecision(BaseModel):
    """一次分析的结构化决策。

    规则（Pydantic 构造时强制，违反 → ValidationError → 服务层翻译为
    ClaimAnalysisMalformedOutput）：
    - relevant=false → claims 必须为空；reason_code 可选；
    - relevant=true → claims 必须 1..5 个；reason_code 必须为 None；
    - 无完全重复 Claim（statement / kinds / refs 全同视为重复）。
    """

    model_config = ConfigDict(frozen=True)

    relevant: bool
    claims: list[ClaimCandidate]
    reason_code: ClaimAnalysisReason | None = None

    @model_validator(mode="after")
    def _validate_rules(self) -> "ClaimAnalysisDecision":
        if not self.relevant and self.claims:
            raise ValueError("relevant=false 时 claims 必须为空")
        if self.relevant and not (1 <= len(self.claims) <= MAX_CLAIMS_PER_DECISION):
            raise ValueError(f"relevant=true 时 claims 必须在 1..{MAX_CLAIMS_PER_DECISION}")
        if self.relevant and self.reason_code is not None:
            raise ValueError("reason_code 仅用于非相关")
        seen: set[tuple] = set()
        for candidate in self.claims:
            key = (
                candidate.statement,
                candidate.claim_kind,
                candidate.confidence,
                candidate.importance,
                tuple(candidate.support_refs),
                tuple(candidate.contradict_refs),
                tuple(candidate.context_refs),
            )
            if key in seen:
                raise ValueError("单 response 不允许完全重复 Claim")
            seen.add(key)
        return self


def normalize_evidence_card_ids(evidence_card_ids: list[UUID]) -> list[UUID]:
    """去重后按 str(uuid) 升序（deterministic canonical order）。

    - 输入必须是 list[UUID]；任一非 UUID / bool → ClaimAnalysisInputError；
    - 去重（保持首次出现）后按 str(uuid) 升序排序 → 与调用方提交顺序无关，
      Evidence Pack 的 E1..En alias 全确定性，ref resolution 可复现。
    """
    if not isinstance(evidence_card_ids, list):
        raise ClaimAnalysisInputError("evidence_card_ids 必须是 list")
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in evidence_card_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise ClaimAnalysisInputError("evidence_card_ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class ClaimAnalysisRequest:
    """调用方提交的分析请求（构造时校验并做 deterministic normalization，不可变）。

    - company_id：UUID；
    - research_question：trim 后非空；
    - analysis_domain：只允许 business / event / risk（其余 →
      ClaimAnalysisDomainNotReady，不提前实现 financial/macro/valuation）；
    - evidence_card_ids：1..MAX_EVIDENCE_PER_REQUEST，去重 + canonical 排序。
    """

    company_id: UUID
    research_question: str
    analysis_domain: ClaimAnalysisDomain
    evidence_card_ids: list[UUID]

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise ClaimAnalysisInputError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise ClaimAnalysisInputError("research_question 不能为空（trim 后）")
        if not isinstance(self.analysis_domain, ClaimAnalysisDomain):
            raise ClaimAnalysisInputError("analysis_domain 必须是 ClaimAnalysisDomain")
        if self.analysis_domain not in _SUPPORTED_DOMAINS_4B1:
            raise ClaimAnalysisDomainNotReady()
        if not self.evidence_card_ids:
            raise ClaimAnalysisInputError("evidence_card_ids 至少 1 条")
        if len(self.evidence_card_ids) > MAX_EVIDENCE_PER_REQUEST:
            raise ClaimAnalysisInputError(f"evidence_card_ids 最多 {MAX_EVIDENCE_PER_REQUEST} 条")
        normalized = normalize_evidence_card_ids(self.evidence_card_ids)
        if not normalized:
            raise ClaimAnalysisInputError("evidence_card_ids 去重后不能为空")
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "evidence_card_ids", normalized)


@dataclass(frozen=True)
class ClaimAnalysisContext:
    """传给模型的本次分析元数据（analysis_domain 由 request 决定，不是 LLM 决定）。

    - research_question：trim 后非空；
    - analysis_domain / strategy：确定性派生，不进入模型输出。
    """

    research_question: str
    analysis_domain: ClaimAnalysisDomain
    strategy: str


@dataclass(frozen=True)
class EvidencePackItem:
    """单条 Evidence 在包中的最小投影（模型输入）。

    **只含必要字段**：evidence_ref（E<number>）/ evidence_statement /
    evidence_type / origin_type / authority_tier / provider_key；document origin
    可附 quote_text / source_published_at / reporting_period_end。
    **不发送**：DB UUID / fingerprint / locator_refs / RawArtifact / 完整
    HTML/PDF / Chroma distance。
    """

    evidence_ref: str
    evidence_statement: str
    evidence_type: str
    origin_type: str
    authority_tier: int
    provider_key: str
    quote_text: str | None = None
    source_published_at: datetime | None = None
    reporting_period_end: date | None = None


@dataclass(frozen=True)
class EvidencePack:
    """本次分析的确定性证据包（E1..En 局部 alias → evidence_card_id 双向映射）。

    - items：按 str(evidence_card_id) 升序编号 E1..En（确定性，与调用方提交顺序无关）；
    - ref_to_card_id：ref → evidence_card_id（ref resolution 用）；
    - card_id_to_ref：evidence_card_id → ref（调试 / 日志用）。
    """

    items: tuple[EvidencePackItem, ...]
    ref_to_card_id: dict[str, UUID]
    card_id_to_ref: dict[UUID, str]


@runtime_checkable
class ClaimAnalysisModel(Protocol):
    """LLM abstraction：把分析上下文 + Evidence Pack 抽成结构化决策。

    - `model_id`：稳定 identifier（provider:model，不伪造 revision）；由
      ClaimAnalysisService 持久化到 Claim.analyst_model_id；
    - `analyze`：接收 ClaimAnalysisContext 与 EvidencePack，返回
      ClaimAnalysisDecision；provider 失败翻译为 ClaimAnalysisModelUnavailable；
    - 实现不得启用 tools / web search / function side effects。
    """

    @property
    def model_id(self) -> str: ...

    async def analyze(
        self,
        context: ClaimAnalysisContext,
        evidence_pack: EvidencePack,
    ) -> ClaimAnalysisDecision: ...


@dataclass(frozen=True)
class ClaimAnalysisResult:
    """一次分析的结果摘要（不含 Claim 正文 / evidence 文本）。

    - relevant：模型判断证据是否与研究问题相关；
    - claim_ids：本次实际创建/回放的 Claim ids（relevant=false → 空）；
    - created_count / replayed_count：新增 / 回放 Claim 数量；
    - reason_code：relevant=false 时可选的 reason_code。
    """

    relevant: bool
    claim_ids: list[UUID]
    created_count: int
    replayed_count: int
    reason_code: ClaimAnalysisReason | None = None
