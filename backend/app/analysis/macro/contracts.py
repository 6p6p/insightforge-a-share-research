"""Structured macro context analysis contracts (stage 4C.1B): request + Pack + LLM Protocol.

Macro Analyst 只做判断——宏观驱动变量如何通过业务渠道传导到公司——**不负责**：
任何定量计算、检索、访问 Chroma、读 RawArtifact、写数据库。确定性交给代码：
M/E alias（MacroDriverPack / CompanyEvidencePack）、ref resolution、macro
numeric-literal guard v1、v6/v3 fingerprint、
MacroClaimService.create_claim_batch 原子持久化。

角色边界（Analyst 只做判断，确定性交给代码）：
- Analyst 负责：判断 Macro Evidence 与公司暴露对研究问题是否相关、生成结构化
  MacroClaimCandidate（statement / claim_kind / confidence / importance /
  channel_type / effect_direction / impact_status / time_alignment / M 编号与
  E 编号引用）；
- 确定性代码负责：MacroDriverPack（M1..Mn）与 CompanyEvidencePack（E1..En）
  构造（两池 namespace 严格分离，各自按 str(uuid) 排序）、ref resolution
  （M → evidence_card_id、E → evidence_card_id）、未知引用 / 跨 relation 冲突
  拒绝、macro numeric guard v1、v6/v3 创建 / replay、
  MacroClaimService.create_claim_batch 原子持久化；
- Analyst **不负责**：Retrieval / 搜索 / 访问 Chroma / 读 RawArtifact / 写数据库。

冻结：
- `MACRO_ANALYST_NAME = "structured_macro_context_analyst"`；
  `MACRO_ANALYST_VERSION = 1`；
  `MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST = 20`；
  `MAX_COMPANY_EVIDENCE_PER_REQUEST = 30`；
  `MAX_CLAIMS_PER_DECISION = 3`。
- MacroClaimCandidate：claim_kind 只允许 inference / risk（**不输出 fact**：
  宏观定量事实由 Macro Evidence 承载；不输出 relative_valuation）；每条 Claim
  ≥1 macro_driver_ref（M 编号）+ ≥1 company_exposure_ref（E 编号）；ref 格式
  M<number> / E<number>；**不输出** UUID / analysis_domain / company_id /
  fingerprint / provenance IDs / chain-of-thought / reasoning_content / Report text。
- MacroAnalysisDecision：relevant=false → claims 必须为空（reason_code 可选）；
  relevant=true → 1..3 claims 且 reason_code 必须为 None。
- overclaim contract（Macro Analyst 特有）：observed_impact 必须带 ≥1
  observed_effect_ref；只有 M+暴露 → 必须 plausible_impact；time_alignment=
  uncertain → 只能是 risk + normal + plausible_impact。
- MacroAnalysisModel（Protocol，定义于 `app/analysis/macro/model.py`）：
  LLM abstraction。domain 不直接依赖具体 provider；自动测试一律用
  FakeMacroAnalysisModel。
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.analysis.macro.errors import MacroAnalysisInputError
from app.claims.contracts import ClaimKind
from app.claims.macro_contracts import (
    MacroChannelType,
    MacroClaimConfidence,
    MacroClaimImportance,
    MacroEffectDirection,
    MacroImpactStatus,
    MacroTimeAlignment,
)

# structured macro context analyst 的身份常量（persisted analyst_name）。
MACRO_ANALYST_NAME = "structured_macro_context_analyst"
MACRO_ANALYST_VERSION = 1

# 单次分析最多 20 条 Macro Evidence（MacroDriver Pack 上限；必填 1..20）。
MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST = 20
# 单次分析最多 30 条 Company Evidence（Company Evidence Pack 上限；必填 1..30）。
MAX_COMPANY_EVIDENCE_PER_REQUEST = 30
# 单次决策最多 3 条 MacroClaimCandidate（→ create_claim_batch 上限）。
MAX_CLAIMS_PER_DECISION = 3

# Macro Analyst 分析重点（宏观变量 → 业务渠道 → 公司暴露；只做判断与综合）。
MACRO_ANALYST_FOCUS = (
    "分析重点：宏观驱动变量（利率 / 汇率 / PMI / 大宗价格 / 宏观观测）如何通过业务渠道"
    "传导到公司（revenue / cost / financing / demand / supply_chain / trade_policy / "
    "operations / other）。只定性解释已提供的 Macro Evidence 与 Company Evidence，"
    "定量事实通过 M / E 编号引用表达；只输出 inference / risk 两类 Claim（宏观定量事实"
    "由 Macro Evidence 承载）；不计算任何宏观指标、不编造数字、不做估值。"
)

# Macro Analyst 输出的 claim_kind 只允许 inference / risk。
# - 不输出 fact：宏观定量事实由 Macro Evidence（source-backed）承载，Analyst 只解释并判断；
# - 不输出 relative_valuation（估值留 4C）。
_ALLOWED_KINDS_MACRO_ANALYST = frozenset((ClaimKind.INFERENCE, ClaimKind.RISK))

_MACRO_REF_PATTERN = re.compile(r"^M\d+$")
_EVIDENCE_REF_PATTERN = re.compile(r"^E\d+$")


class MacroAnalysisReason(StrEnum):
    """reason_code：仅用于非相关（relevant=false），可选。

    不允许出现 prediction / recommendation / buy / sell。
    """

    NOT_RELEVANT = "not_relevant"
    INSUFFICIENT_MACRO_EVIDENCE = "insufficient_macro_evidence"
    INSUFFICIENT_COMPANY_EVIDENCE = "insufficient_company_evidence"


class MacroClaimCandidate(BaseModel):
    """单条 Macro Claim 候选（Pydantic 结构化输出，模型生成）。

    - statement：trim 后非空；**禁止包含数字 / 百分比 / 中文定量表达**（macro
      numeric-literal guard v1 在服务层执行，schema 不自动删数字、不改写）；
    - claim_kind：只允许 inference / risk（schema 层拒绝 fact 与
      relative_valuation；宏观定量事实由 Macro Evidence 承载）；
    - channel_type：复用 MacroChannelType（描述传导渠道，不是宏观变量本身）；
    - macro_driver_refs：全部 M<number> 格式；company_exposure_refs /
      observed_effect_refs / additional_*_evidence_refs：全部 E<number> 格式
      （M 编号不能放进 Evidence list、E 编号不能放进 MacroDriver list——格式
      不同，混入即 schema 拒绝）；
    - 每条 Claim ≥1 macro_driver_ref + ≥1 company_exposure_ref；
    - overclaim contract（schema 层强制）：observed_impact 必须带 ≥1
      observed_effect_ref；time_alignment=uncertain 只能是 risk + normal +
      plausible_impact；
    - **无 reasoning / chain-of-thought / analysis_domain / company_id /
      fingerprint / provenance IDs / Report text**。
    """

    model_config = ConfigDict(frozen=True)

    statement: str
    claim_kind: ClaimKind
    confidence: MacroClaimConfidence
    importance: MacroClaimImportance
    channel_type: MacroChannelType
    effect_direction: MacroEffectDirection
    impact_status: MacroImpactStatus
    time_alignment: MacroTimeAlignment
    macro_driver_refs: list[str]
    company_exposure_refs: list[str]
    observed_effect_refs: list[str]
    additional_support_evidence_refs: list[str]
    additional_contradict_evidence_refs: list[str]
    additional_context_evidence_refs: list[str]

    @model_validator(mode="after")
    def _validate(self) -> "MacroClaimCandidate":
        if not self.statement.strip():
            raise ValueError("statement 不能为空（trim 后）")
        m_groups = (self.macro_driver_refs,)
        e_groups = (
            self.company_exposure_refs,
            self.observed_effect_refs,
            self.additional_support_evidence_refs,
            self.additional_contradict_evidence_refs,
            self.additional_context_evidence_refs,
        )
        for ref in (ref for group in m_groups for ref in group):
            if not _MACRO_REF_PATTERN.fullmatch(ref):
                raise ValueError(f"macro driver ref 必须是 M<number> 格式: {ref!r}")
        for ref in (ref for group in e_groups for ref in group):
            if not _EVIDENCE_REF_PATTERN.fullmatch(ref):
                raise ValueError(f"evidence ref 必须是 E<number> 格式: {ref!r}")
        if not self.macro_driver_refs:
            raise ValueError("每条 Claim 至少 1 个 macro_driver_ref")
        if not self.company_exposure_refs:
            raise ValueError("每条 Claim 至少 1 个 company_exposure_ref")
        if self.claim_kind not in _ALLOWED_KINDS_MACRO_ANALYST:
            raise ValueError("macro analyst 只允许 inference / risk")
        for group in m_groups + e_groups:
            if len(group) != len(set(group)):
                raise ValueError("同 relation 组内不允许重复 ref")
        # overclaim contract（schema 层强制）：
        if (
            self.impact_status == MacroImpactStatus.OBSERVED_IMPACT
            and not self.observed_effect_refs
        ):
            raise ValueError(
                "observed_impact 需要 ≥1 observed_effect_ref（否则只能 plausible_impact）"
            )
        if self.time_alignment == MacroTimeAlignment.UNCERTAIN and (
            self.claim_kind != ClaimKind.RISK
            or self.importance != MacroClaimImportance.NORMAL
            or self.impact_status != MacroImpactStatus.PLAUSIBLE_IMPACT
        ):
            raise ValueError(
                "time_alignment=uncertain 只允许 plausible_impact + risk + normal"
            )
        return self


class MacroAnalysisDecision(BaseModel):
    """一次 macro 分析的结构化决策。

    规则（Pydantic 构造时强制，违反 → ValidationError → 服务层翻译为
    MacroAnalysisMalformedOutput）：
    - relevant=false → claims 必须为空；reason_code 可选；
    - relevant=true → claims 必须 1..3 个；reason_code 必须为 None；
    - 无完全重复 Claim。
    """

    model_config = ConfigDict(frozen=True)

    relevant: bool
    claims: list[MacroClaimCandidate]
    reason_code: MacroAnalysisReason | None = None

    @model_validator(mode="after")
    def _validate_rules(self) -> "MacroAnalysisDecision":
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
                candidate.channel_type,
                candidate.effect_direction,
                candidate.impact_status,
                candidate.time_alignment,
                tuple(candidate.macro_driver_refs),
                tuple(candidate.company_exposure_refs),
                tuple(candidate.observed_effect_refs),
                tuple(candidate.additional_support_evidence_refs),
                tuple(candidate.additional_contradict_evidence_refs),
                tuple(candidate.additional_context_evidence_refs),
            )
            if key in seen:
                raise ValueError("单 response 不允许完全重复 Claim")
            seen.add(key)
        return self


def normalize_macro_evidence_ids(evidence_card_ids: list[UUID]) -> list[UUID]:
    """去重后按 str(uuid) 升序（deterministic canonical order）。"""
    if not isinstance(evidence_card_ids, list):
        raise MacroAnalysisInputError("evidence_card_ids 必须是 list")
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in evidence_card_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise MacroAnalysisInputError("evidence_card_ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class MacroAnalysisRequest:
    """调用方提交的 macro 分析请求（构造时校验并做 deterministic normalization）。

    - company_id：UUID；
    - research_question：trim 后非空；
    - analysis_as_of：**必填 date**（分析基准日，用于 no-lookahead 硬边界）；
    - macro_driver_evidence_ids：1..MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST，
      去重 + canonical 排序；
    - company_evidence_ids：1..MAX_COMPANY_EVIDENCE_PER_REQUEST，
      去重 + canonical 排序；
    - 两池 namespace 严格分离：同一 evidence 不能同时出现在两个池（构造时拒绝）；
    - 所有对象必须属于 request.company_id（Service 从真实 PG 校验）。
    """

    company_id: UUID
    research_question: str
    analysis_as_of: date
    macro_driver_evidence_ids: list[UUID]
    company_evidence_ids: list[UUID]

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise MacroAnalysisInputError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise MacroAnalysisInputError("research_question 不能为空（trim 后）")
        if isinstance(self.analysis_as_of, bool) or not isinstance(self.analysis_as_of, date):
            raise MacroAnalysisInputError("analysis_as_of 必须是 date")
        if not isinstance(self.macro_driver_evidence_ids, list):
            raise MacroAnalysisInputError("macro_driver_evidence_ids 必须是 list")
        if not isinstance(self.company_evidence_ids, list):
            raise MacroAnalysisInputError("company_evidence_ids 必须是 list")
        if len(self.macro_driver_evidence_ids) > MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST:
            raise MacroAnalysisInputError(
                f"macro_driver_evidence_ids 最多 {MAX_MACRO_DRIVER_EVIDENCE_PER_REQUEST} 条"
            )
        if len(self.company_evidence_ids) > MAX_COMPANY_EVIDENCE_PER_REQUEST:
            raise MacroAnalysisInputError(
                f"company_evidence_ids 最多 {MAX_COMPANY_EVIDENCE_PER_REQUEST} 条"
            )
        drivers = normalize_macro_evidence_ids(self.macro_driver_evidence_ids)
        companies = normalize_macro_evidence_ids(self.company_evidence_ids)
        if not drivers:
            raise MacroAnalysisInputError("macro_driver_evidence_ids 去重后不能为空")
        if not companies:
            raise MacroAnalysisInputError("company_evidence_ids 去重后不能为空")
        if set(drivers) & set(companies):
            raise MacroAnalysisInputError("macro_driver 池与 company 池不能重叠")
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "macro_driver_evidence_ids", drivers)
        object.__setattr__(self, "company_evidence_ids", companies)


@dataclass(frozen=True)
class MacroAnalysisContext:
    """传给模型的本次分析元数据（analysis_domain 固定为 macro，不是 LLM 决定）。"""

    research_question: str
    analysis_as_of: date
    strategy: str


@dataclass(frozen=True)
class MacroAnalysisResult:
    """一次 macro 分析的结果摘要（不含 Claim 正文 / evidence 文本 / 数值细节）。

    - relevant：模型判断结果是否与研究问题相关；
    - claim_ids：本次实际创建/回放的 Claim ids（relevant=false → 空）；
    - created_count / replayed_count：新增 / 回放 Claim 数量；
    - reason_code：relevant=false 时可选的 reason_code。
    """

    relevant: bool
    claim_ids: list[UUID]
    created_count: int
    replayed_count: int
    reason_code: MacroAnalysisReason | None = None
