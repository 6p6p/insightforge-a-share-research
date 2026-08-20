"""Structured financial analysis contracts (stage 4B.2C.2): request + Pack + LLM Protocol.

Financial Analyst 只做判断——趋势 / 盈利能力 / 现金流与资产负债结构 / 风险提示 /
多项财务指标结果的综合——**不负责**：计算任何财务指标、修改公式结果、生成
Report、宏观因果、估值。确定性交给代码：C/E alias（Calculation/Evidence Pack）、
ref resolution、numeric-literal guard、v3 fingerprint、
FinancialClaimService.create_claim_batch 原子持久化。

角色边界（Analyst 只做判断，确定性交给代码）：
- Analyst 负责：判断 Calculation 与研究问题的相关性、生成结构化
  FinancialClaimCandidate（statement / claim_kind / confidence / importance /
  C 编号与 E 编号引用）；
- 确定性代码负责：Calculation/Evidence Pack 构造（C1..Cn / E1..En 局部 alias，
  按 str(uuid) 排序）、ref resolution（C → calculation_id、E →
  evidence_card_id）、未知引用 / 跨 relation 冲突拒绝、numeric-literal guard、
  v3 创建 / replay、FinancialClaimService.create_claim_batch 原子持久化；
- Analyst **不负责**：Retrieval / 搜索 / 访问 Chroma / 读 RawArtifact / 写数据库。

冻结：
- `FINANCIAL_ANALYST_NAME = "structured_financial_analyst"`；
  `FINANCIAL_ANALYST_VERSION = 2`（v2 = V1.1 closure 数字自检清单：statement
  输出前逐条核对数字/百分号/中文数字/定量短语并删除——修复生产实测模型反复
  输出数字字面量触发 FinancialAnalysisNumericLiteralForbidden）；
  `MAX_CALCULATIONS_PER_REQUEST = 20`；`MAX_EVIDENCE_PER_REQUEST = 20`；
  `MAX_CLAIMS_PER_DECISION = 3`。
- FinancialClaimCandidate：claim_kind 只允许 inference / risk（**不输出 fact**：
  定量事实由 FinancialCalculation 承担；不输出 relative_valuation）；每条 Claim
  ≥1 support_calculation_ref；ref 格式 C<number> / E<number>；**不输出** UUID /
  analysis_domain / company_id / formula / result_value rewrite / fingerprint /
  provenance IDs / chain-of-thought / reasoning_content / Report text。
- FinancialAnalysisDecision：relevant=false → claims 必须为空（reason_code 可选）；
  relevant=true → 1..3 claims 且 reason_code 必须为 None。
- FinancialAnalysisModel（Protocol）：LLM abstraction。domain 不直接依赖具体
  provider；自动测试一律用 FakeFinancialAnalysisModel。
"""

import re
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from typing import Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.analysis.claims.contracts import EvidencePack
from app.analysis.financial.errors import FinancialAnalysisInputError
from app.claims.contracts import ClaimAnalysisDomain, ClaimKind
from app.claims.financial_contracts import (
    FinancialClaimConfidence,
    FinancialClaimImportance,
)

# structured financial analyst 的身份常量（persisted analyst_name）。
FINANCIAL_ANALYST_NAME = "structured_financial_analyst"
FINANCIAL_ANALYST_VERSION = 2

# 单次分析最多 20 条 Calculation（Calculation Pack 上限）。
MAX_CALCULATIONS_PER_REQUEST = 20
# 单次分析最多 20 条 additional Evidence（Evidence Pack 上限；0 条允许）。
MAX_EVIDENCE_PER_REQUEST = 20
# 单次决策最多 3 条 FinancialClaimCandidate（→ create_claim_batch 上限）。
MAX_CLAIMS_PER_DECISION = 3

# Financial Analyst 分析重点（趋势 / 盈利能力 / 现金流资产负债 / 风险提示 /
# 多项结果综合；不负责计算 / 宏观因果 / 估值）。
FINANCIAL_ANALYST_FOCUS = (
    "分析重点：趋势判断、盈利能力、现金流与资产负债结构、风险提示、多项财务指标结果的"
    "综合解释。只定性解释已计算的 Financial Calculation，定量事实通过 C 编号引用表达；"
    "只输出 inference / risk 两类 Claim（fact 由确定性 Calculation 承担）；不计算任何"
    "财务指标、不修改公式结果、不做估值。"
)

# Financial Analyst 输出的 claim_kind 只允许 inference / risk。
# - 不输出 fact：定量事实由 FinancialCalculation（确定性计算）承担，Analyst 只解释并判断；
# - 不输出 relative_valuation（估值留 4C）。
# FinancialClaimDraft（更低层 domain contract）仍保留 fact 支持，供确定性 producer 使用。
_ALLOWED_KINDS_FINANCIAL_ANALYST = frozenset((ClaimKind.INFERENCE, ClaimKind.RISK))

_CALCULATION_REF_PATTERN = re.compile(r"^C\d+$")
_EVIDENCE_REF_PATTERN = re.compile(r"^E\d+$")


class FinancialAnalysisReason(StrEnum):
    """reason_code：仅用于非相关（relevant=false），可选。

    不允许出现 prediction / recommendation / buy / sell。
    """

    NOT_RELEVANT = "not_relevant"
    INSUFFICIENT_CALCULATIONS = "insufficient_calculations"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    # 自动修复（Part 1 Hardening）：numeric-literal 违规 repair 重试耗尽后
    # 降级为 0-claims 定性结果（不引入无来源数字；warning 记录在日志/审计）。
    NUMERIC_REFERENCE_DOWNGRADED = "numeric_reference_downgraded"


class FinancialClaimCandidate(BaseModel):
    """单条 Financial Claim 候选（Pydantic 结构化输出，模型生成）。

    - statement：trim 后非空；**禁止包含数字 / 百分比**（numeric-literal
      guard 在服务层执行，schema 不自动删数字、不改写）；
    - claim_kind：只允许 inference / risk（schema 层拒绝 fact 与
      relative_valuation；fact 由确定性 Calculation 承担）；
    - support|contradict|context_calculation_refs：全部 C<number> 格式；
    - additional_support|contradict|context_evidence_refs：全部 E<number> 格式
      （C 编号不能放进 Evidence list、E 编号不能放进 Calculation list——格式
      不同，混入即 schema 拒绝）；
    - 每条 Claim ≥1 support_calculation_ref；
    - **无 reasoning / chain-of-thought / analysis_domain / company_id /
      formula / result_value rewrite / fingerprint / provenance IDs / Report text**。
    """

    model_config = ConfigDict(frozen=True)

    statement: str
    claim_kind: ClaimKind
    confidence: FinancialClaimConfidence
    importance: FinancialClaimImportance
    support_calculation_refs: list[str]
    contradict_calculation_refs: list[str]
    context_calculation_refs: list[str]
    additional_support_evidence_refs: list[str]
    additional_contradict_evidence_refs: list[str]
    additional_context_evidence_refs: list[str]

    @model_validator(mode="after")
    def _validate(self) -> "FinancialClaimCandidate":
        if not self.statement.strip():
            raise ValueError("statement 不能为空（trim 后）")
        calc_groups = (
            self.support_calculation_refs,
            self.contradict_calculation_refs,
            self.context_calculation_refs,
        )
        ev_groups = (
            self.additional_support_evidence_refs,
            self.additional_contradict_evidence_refs,
            self.additional_context_evidence_refs,
        )
        for ref in (ref for group in calc_groups for ref in group):
            if not _CALCULATION_REF_PATTERN.fullmatch(ref):
                raise ValueError(f"calculation ref 必须是 C<number> 格式: {ref!r}")
        for ref in (ref for group in ev_groups for ref in group):
            if not _EVIDENCE_REF_PATTERN.fullmatch(ref):
                raise ValueError(f"evidence ref 必须是 E<number> 格式: {ref!r}")
        if not self.support_calculation_refs:
            raise ValueError("每条 Claim 至少 1 个 support_calculation_ref")
        if self.claim_kind not in _ALLOWED_KINDS_FINANCIAL_ANALYST:
            raise ValueError("financial analyst 只允许 inference / risk")
        for group in calc_groups + ev_groups:
            if len(group) != len(set(group)):
                raise ValueError("同 relation 组内不允许重复 ref")
        return self


class FinancialAnalysisDecision(BaseModel):
    """一次财务分析的结构化决策。

    规则（Pydantic 构造时强制，违反 → ValidationError → 服务层翻译为
    FinancialAnalysisMalformedOutput）：
    - relevant=false → claims 必须为空；reason_code 可选；
    - relevant=true → claims 必须 1..3 个；reason_code 必须为 None；
    - 无完全重复 Claim。
    """

    model_config = ConfigDict(frozen=True)

    relevant: bool
    claims: list[FinancialClaimCandidate]
    reason_code: FinancialAnalysisReason | None = None

    @model_validator(mode="after")
    def _validate_rules(self) -> "FinancialAnalysisDecision":
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
                tuple(candidate.support_calculation_refs),
                tuple(candidate.contradict_calculation_refs),
                tuple(candidate.context_calculation_refs),
                tuple(candidate.additional_support_evidence_refs),
                tuple(candidate.additional_contradict_evidence_refs),
                tuple(candidate.additional_context_evidence_refs),
            )
            if key in seen:
                raise ValueError("单 response 不允许完全重复 Claim")
            seen.add(key)
        return self


def normalize_calculation_ids(calculation_ids: list[UUID]) -> list[UUID]:
    """去重后按 str(uuid) 升序（deterministic canonical order）。

    - 输入必须是 list[UUID]；任一非 UUID / bool → FinancialAnalysisInputError；
    - 去重（保持首次出现）后按 str(uuid) 升序排序 → 与调用方提交顺序无关，
      Calculation Pack 的 C1..Cn alias 全确定性，ref resolution 可复现。
    """
    if not isinstance(calculation_ids, list):
        raise FinancialAnalysisInputError("calculation_ids 必须是 list")
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in calculation_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise FinancialAnalysisInputError("calculation_ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


def normalize_evidence_card_ids(evidence_card_ids: list[UUID]) -> list[UUID]:
    """去重后按 str(uuid) 升序（deterministic canonical order）。"""
    if not isinstance(evidence_card_ids, list):
        raise FinancialAnalysisInputError("additional_evidence_ids 必须是 list")
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in evidence_card_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise FinancialAnalysisInputError("additional_evidence_ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class FinancialAnalysisRequest:
    """调用方提交的财务分析请求（构造时校验并做 deterministic normalization）。

    - company_id：UUID；
    - research_question：trim 后非空；
    - calculation_ids：1..MAX_CALCULATIONS_PER_REQUEST，去重 + canonical 排序；
    - additional_evidence_ids：0..MAX_EVIDENCE_PER_REQUEST，去重 + canonical 排序；
    - 所有对象必须属于 request.company_id（Service 从真实 PG 校验）。
    """

    company_id: UUID
    research_question: str
    calculation_ids: list[UUID]
    additional_evidence_ids: list[UUID] = field(default_factory=list)
    analysis_as_of: date | None = None

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise FinancialAnalysisInputError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise FinancialAnalysisInputError("research_question 不能为空（trim 后）")
        if not isinstance(self.calculation_ids, list) or not self.calculation_ids:
            raise FinancialAnalysisInputError("calculation_ids 至少 1 条")
        if len(self.calculation_ids) > MAX_CALCULATIONS_PER_REQUEST:
            raise FinancialAnalysisInputError(
                f"calculation_ids 最多 {MAX_CALCULATIONS_PER_REQUEST} 条"
            )
        normalized_calcs = normalize_calculation_ids(self.calculation_ids)
        if not normalized_calcs:
            raise FinancialAnalysisInputError("calculation_ids 去重后不能为空")
        if not isinstance(self.additional_evidence_ids, list):
            raise FinancialAnalysisInputError("additional_evidence_ids 必须是 list")
        if len(self.additional_evidence_ids) > MAX_EVIDENCE_PER_REQUEST:
            raise FinancialAnalysisInputError(
                f"additional_evidence_ids 最多 {MAX_EVIDENCE_PER_REQUEST} 条"
            )
        normalized_evidence = normalize_evidence_card_ids(self.additional_evidence_ids)
        if self.analysis_as_of is not None and not isinstance(self.analysis_as_of, date):
            raise FinancialAnalysisInputError("analysis_as_of 必须是 date 或 None")
        object.__setattr__(self, "analysis_as_of", self.analysis_as_of)
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "calculation_ids", normalized_calcs)
        object.__setattr__(self, "additional_evidence_ids", normalized_evidence)


@dataclass(frozen=True)
class FinancialAnalysisContext:
    """传给模型的本次分析元数据（analysis_domain 固定为 financial，不是 LLM 决定）。"""

    research_question: str
    strategy: str
    analysis_domain: str = ClaimAnalysisDomain.FINANCIAL.value


@dataclass(frozen=True)
class InputSummaryItem:
    """单条 Calculation 输入的摘要（模型输入；单位恒为 CNY）。

    提供 role / metric_code / period_start / period_end /
    normalized_value_cny（按存储表达）/ unit=CNY。**不发送** observation UUID。
    """

    role: str
    metric_code: str
    period_start: str | None
    period_end: str
    normalized_value_cny: str
    unit: str = "CNY"


@dataclass(frozen=True)
class CalculationPackItem:
    """单条 Calculation 在包中的最小投影（模型输入）。

    **只含必要字段**：calculation_ref（C<number>）/ calculation_code /
    result_value（按存储表达，ratio 存 0.2 不是让 LLM 算 20%）/ result_unit /
    formula_version / period_summary（确定性）/ statement_scope /
    deterministic_display_value（程序生成，仅供阅读）/ inputs（input 摘要）。
    **不发送**：calculation UUID / observation UUID / fingerprint / Evidence
    UUID / DB IDs / RawArtifact / locator / Chroma。
    """

    calculation_ref: str
    calculation_code: str
    result_value: str
    result_unit: str
    formula_version: int
    period_summary: str
    statement_scope: str
    deterministic_display_value: str
    inputs: tuple[InputSummaryItem, ...]


@dataclass(frozen=True)
class CalculationPack:
    """本次分析的确定性 Calculation 包（C1..Cn 局部 alias → calculation_id 双向映射）。

    - items：按 str(calculation_id) 升序编号 C1..Cn（确定性，与调用方提交顺序无关）；
    - ref_to_calc_id：ref → calculation_id（ref resolution 用）；
    - calc_id_to_ref：calculation_id → ref（调试 / 日志用）。
    """

    items: tuple[CalculationPackItem, ...]
    ref_to_calc_id: dict[str, UUID]
    calc_id_to_ref: dict[UUID, str]


@runtime_checkable
class FinancialAnalysisModel(Protocol):
    """LLM abstraction：把分析上下文 + Calculation Pack + Evidence Pack 抽成结构化决策。

    - `model_id`：稳定 identifier（provider:model，不伪造 revision）；由
      FinancialAnalysisService 持久化到 Claim.analyst_model_id；
    - `analyze`：接收 FinancialAnalysisContext 与 Calculation/Evidence Pack，
      返回 FinancialAnalysisDecision；provider 失败翻译为
      FinancialAnalysisModelUnavailable；
    - 实现不得启用 tools / web search / function side effects。
    """

    @property
    def model_id(self) -> str: ...

    async def analyze(
        self,
        context: FinancialAnalysisContext,
        calculation_pack: CalculationPack,
        evidence_pack: EvidencePack,
        correction_hint: str | None = None,
    ) -> FinancialAnalysisDecision: ...


@dataclass(frozen=True)
class FinancialAnalysisResult:
    """一次财务分析的结果摘要（不含 Claim 正文 / evidence 文本 / 数值细节）。

    - relevant：模型判断结果是否与研究问题相关；
    - claim_ids：本次实际创建/回放的 Claim ids（relevant=false → 空）；
    - created_count / replayed_count：新增 / 回放 Claim 数量；
    - reason_code：relevant=false 时可选的 reason_code。
    """

    relevant: bool
    claim_ids: list[UUID]
    created_count: int
    replayed_count: int
    reason_code: FinancialAnalysisReason | None = None
