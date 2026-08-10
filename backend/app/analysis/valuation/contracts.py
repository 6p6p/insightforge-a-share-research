"""Structured Relative Valuation analysis contracts (stage 4C.2B.2): request + Decision + Protocol.

Valuation Analyst 只做判断——对程序已算好的 PE/PB/PS 相对估值比较给出
assessment（relative_high / broadly_in_line / relative_low / mixed / uncertain）、
confidence、importance 与 comparison relations——**不负责**：任何数值计算（median /
premium / percent）、peer 选择、数值生成、Claim statement 生成、target price /
fair value / 交易建议。确定性交给代码：V alias（V1..Vn）、ref resolution、
no-cherry-picking coverage、direction consistency、确定性 statement 渲染、
ValuationClaimService.create_claim 原子持久化。

角色边界（Analyst 只做判断，确定性交给代码）：
- Analyst 负责：relevant 判断、assessment / confidence / importance、每个
  comparison 归入 supports / contradicts / context（V 编号引用）；
- 确定性代码负责：V1..Vn alias 构造（按 metric_code pe_ttm/pb_mrq/ps_ttm 排序）、
  ref resolution（V → comparison_id）、未知引用 / 跨 relation 冲突 / 遗漏 input
  comparison 拒绝、direction consistency（无 hidden thresholds）、uncertain
  importance policy、`render_valuation_claim_statement(assessment)` 确定性渲染、
  v7 Claim 创建 / replay；
- Analyst **不负责**：Retrieval / 搜索 / 访问 Chroma / 读 RawArtifact / 写数据库 /
  计算任何数值 / 生成 Claim statement。

冻结：
- `VALUATION_ANALYST_NAME = "structured_relative_valuation_analyst"`；
  `VALUATION_ANALYST_VERSION = 1`；production model_id =
  `deepseek:deepseek-v4-flash`（thinking disabled / temperature=0 / structured
  output / 无 tools / 无 web search）。
- `ValuationAnalysisRequest`：company_id + research_question（trim 非空）+
  analysis_as_of + comparison_ids（**1..3**，去重 + canonical sort）。**不接**
  additional Evidence（v1 Valuation Analyst 只做纯相对估值判断；成长性 / 盈利
  质量 / 业务背景留给 4D Claim Synthesis 结合 Financial / Business / Macro /
  Risk Claim，防止 Valuation Agent 再次发明基本面事实）。
- `ValuationAnalysisDecision`：relevant=false → assessment / confidence /
  importance 全 None、全部 refs 空、reason_code 可选（not_relevant /
  insufficient_comparisons / insufficient_consistency）；relevant=true →
  assessment / confidence / importance **必填**、support refs >= 1、
  reason_code=None。ref 格式 V<number>（只存在的 comparison 编号）。
- 本阶段一个 request **最多生成 1 个 Valuation Claim**（一个明确 peer universe +
  同一日期 + PE/PB/PS 应综合成一个 relative valuation assessment）。
"""

import re
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.analysis.valuation.errors import ValuationAnalysisInputError
from app.valuation.claim_contracts import (
    ValuationClaimAssessment,
    ValuationClaimConfidence,
    ValuationClaimImportance,
)

# structured relative valuation analyst 的身份常量（persisted analyst_name）。
VALUATION_ANALYST_NAME = "structured_relative_valuation_analyst"
VALUATION_ANALYST_VERSION = 1

# 单次分析 1..3 条 comparison（v1 只有 PE / PB / PS，每个 metric 最多一个）。
MIN_VALUATION_COMPARISONS_PER_REQUEST = 1
MAX_VALUATION_COMPARISONS_PER_REQUEST = 3

# Valuation Analyst 分析策略（判断 + relations；不计算 / 不选 peer / 不估值）。
VALUATION_ANALYST_FOCUS = (
    "分析重点：在给定 research question 下对程序已计算的 PE/PB/PS 相对估值比较"
    "（V1..Vn）给出方向性 assessment、confidence、importance，并把每条 comparison "
    "归入 supports / contradicts / context。所有数值（target / median / premium / "
    "position）均为程序已计算，只读不重算；不选择 peers；不做 target price / fair "
    "value / 买卖建议。"
)

# V ref 格式：V<number>（只存在的 comparison 编号，alias 由代码确定性分配）。
_V_REF_PATTERN = re.compile(r"^V\d+$")


class ValuationAnalysisReason(StrEnum):
    """reason_code：仅用于非相关（relevant=false），可选。

    不允许出现 prediction / recommendation / buy / sell。
    """

    NOT_RELEVANT = "not_relevant"
    INSUFFICIENT_COMPARISONS = "insufficient_comparisons"
    INSUFFICIENT_CONSISTENCY = "insufficient_consistency"


class ValuationAnalysisDecision(BaseModel):
    """一次 relative valuation 分析的结构化决策（Pydantic 结构化输出，模型生成）。

    规则（Pydantic 构造时强制，违反 → ValidationError → 服务层翻译为
    ValuationAnalysisMalformedOutput）：
    - relevant=false → assessment / confidence / importance 全 None、全部 refs 空、
      reason_code 可选（枚举限定）；
    - relevant=true → assessment / confidence / importance **必填**、support refs
      >= 1、reason_code=None；
    - 全部 V ref 必须是 V<number> 格式；同 relation 组内不允许重复。
    服务层继续校验：未知 ref / 跨 relation / 遗漏 input comparison / direction
    consistency / uncertain importance policy（需要 pack 上下文）。
    """

    model_config = ConfigDict(frozen=True)

    relevant: bool
    assessment: ValuationClaimAssessment | None = None
    confidence: ValuationClaimConfidence | None = None
    importance: ValuationClaimImportance | None = None
    support_comparison_refs: list[str] = []
    contradict_comparison_refs: list[str] = []
    context_comparison_refs: list[str] = []
    reason_code: ValuationAnalysisReason | None = None

    @model_validator(mode="after")
    def _validate_rules(self) -> "ValuationAnalysisDecision":
        if not self.relevant:
            if (
                self.assessment is not None
                or self.confidence is not None
                or self.importance is not None
            ):
                raise ValueError("relevant=false 时 assessment/confidence/importance 必须为 None")
            if (
                self.support_comparison_refs
                or self.contradict_comparison_refs
                or self.context_comparison_refs
            ):
                raise ValueError("relevant=false 时全部 comparison refs 必须为空")
        else:
            if self.assessment is None or self.confidence is None or self.importance is None:
                raise ValueError("relevant=true 时 assessment/confidence/importance 必须提供")
            if not self.support_comparison_refs:
                raise ValueError("relevant=true 时 support refs >= 1")
            if self.reason_code is not None:
                raise ValueError("reason_code 仅用于非相关")
        for ref in (
            self.support_comparison_refs
            + self.contradict_comparison_refs
            + self.context_comparison_refs
        ):
            if not _V_REF_PATTERN.fullmatch(ref):
                raise ValueError(f"comparison ref 必须是 V<number> 格式: {ref!r}")
        for group in (
            self.support_comparison_refs,
            self.contradict_comparison_refs,
            self.context_comparison_refs,
        ):
            if len(group) != len(set(group)):
                raise ValueError("同 relation 组内不允许重复 ref")
        return self


def normalize_comparison_ids(comparison_ids: list[UUID]) -> list[UUID]:
    """comparison id 列表去重后按 canonical 顺序排序（deterministic）。"""
    if not isinstance(comparison_ids, list):
        raise ValuationAnalysisInputError("comparison_ids 必须是 list")
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in comparison_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise ValuationAnalysisInputError("comparison_ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class ValuationAnalysisRequest:
    """调用方提交的 relative valuation 分析请求（构造时校验并做 deterministic normalization）。

    - company_id：UUID；
    - research_question：trim 后非空；
    - analysis_as_of：**必填 date**（分析基准日，与全部 comparison.analysis_as_of
      完全一致——Service 校验）；
    - comparison_ids：1..MAX_VALUATION_COMPARISONS_PER_REQUEST，去重 + canonical
      排序；
    - **不接 additional Evidence**（v1 Valuation Analyst 只做纯相对估值判断）。
    """

    company_id: UUID
    research_question: str
    analysis_as_of: date
    comparison_ids: list[UUID]

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise ValuationAnalysisInputError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise ValuationAnalysisInputError("research_question 不能为空（trim 后）")
        if isinstance(self.analysis_as_of, bool) or not isinstance(self.analysis_as_of, date):
            raise ValuationAnalysisInputError("analysis_as_of 必须是 date")
        ids = normalize_comparison_ids(self.comparison_ids)
        if not (
            MIN_VALUATION_COMPARISONS_PER_REQUEST
            <= len(ids)
            <= MAX_VALUATION_COMPARISONS_PER_REQUEST
        ):
            raise ValuationAnalysisInputError(
                f"comparison_ids 必须在 {MIN_VALUATION_COMPARISONS_PER_REQUEST}.."
                f"{MAX_VALUATION_COMPARISONS_PER_REQUEST} 条（去重后）"
            )
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "comparison_ids", ids)


@dataclass(frozen=True)
class ValuationAnalysisContext:
    """传给模型的本次分析元数据（analysis_domain 固定为 valuation，不是 LLM 决定）。"""

    research_question: str
    analysis_as_of: date
    strategy: str


@dataclass(frozen=True)
class ValuationAnalysisResult:
    """一次 valuation 分析的结果摘要（不含 Claim 正文 / pack 细节 / alias 映射）。

    - relevant：模型判断结果是否与研究问题相关；
    - claim_id：本次实际创建 / 回放的 Claim id（relevant=false → None）；
    - replayed：True=复用既有 fingerprint 的 Claim，False=本次真正新增；
    - assessment：relevant=true 时的 analysis assessment；
    - reason_code：relevant=false 时可选的 reason_code。
    **不返回** raw provider response / reasoning / prompt / UUID alias 映射。
    """

    relevant: bool
    claim_id: UUID | None
    replayed: bool
    assessment: ValuationClaimAssessment | None = None
    reason_code: ValuationAnalysisReason | None = None
