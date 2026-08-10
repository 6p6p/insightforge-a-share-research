"""Relative Valuation Claim contracts (stage 4C.2B.1): schema v7 + draft + fingerprint.

4C.2B.1 持久化 **Claim ↔ RelativeValuationComparison** 链接
（Claim → ClaimRelativeValuationComparisonLink → RelativeValuationComparison →
ValuationMetricObservation → EvidenceCard → Source），使 Audit 可重算 peer
median / premium 并知道 judgment 基于哪些 peer comparisons。**不把
RelativeValuationComparison 伪装成 EvidenceCard**：Comparison = derived
deterministic fact，EvidenceCard = source-backed fact，保持分层。

本模块冻结：
- `VALUATION_CLAIM_SCHEMA_VERSION = 7`——claims.claim_schema_version 的当前值
  （valuation Claim；含 Comparison links + Claim profile）。**不改** generic
  Claim v1 / Financial v2/v3 / Macro v4/v5/v6。
- `VALUATION_CLAIM_PROFILE_SCHEMA_VERSION = 1`——relative_valuation_claim_profiles
  .profile_schema_version 的当前值（assessment / analysis_as_of 语义版本）。
- `ValuationClaimAssessment`（relative_high / broadly_in_line / relative_low /
  mixed / uncertain）：**分析判断**，不是程序从 premium 自动推导的公式输出；
  程序不写 hidden thresholds（premium>20%→relative_high 之类）；**不做**
  buy/sell/bullish/bearish/cheap/expensive。
- `ValuationClaimDraft`：**专用新类，不污染 ClaimDraft / FinancialClaimDraft**。
  语义输入 = company_id / research_question / analysis_as_of / statement /
  assessment / confidence / importance / support/contradict/context comparison
  ids / additional support/contradict/context evidence ids / analyst_name /
  analyst_version / analyst_model_id optional。**固定 analysis_domain=valuation、
  claim_kind=relative_valuation**（draft 不提供这两个字段，Service 强制）。
  至少 1 个 support_comparison_id。同一 comparison 不能跨 relation 重复。
- v7 fingerprint：claim_schema_version + profile_schema_version + claim
  semantic fields + assessment + analysis_as_of + analyst 身份 + 按 relation
  分组的 comparison 组（comparison_id + comparison_fingerprint）+ 按 relation
  分组的 evidence 组（evidence_card_id + evidence_fingerprint，含自动展开的
  source Evidence 与 additional Evidence）。**不含** claim_id / created_at。
"""

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from app.valuation.claim_errors import ValuationClaimDraftError

# claims.claim_schema_version 的当前值（valuation Claim；含 Comparison links +
# Profile）。
VALUATION_CLAIM_SCHEMA_VERSION = 7
# relative_valuation_claim_profiles.profile_schema_version 的当前值。
VALUATION_CLAIM_PROFILE_SCHEMA_VERSION = 1

# 一个 claim 内 v1 最多 3 个 comparison（PE / PB / PS 各最多 1 个）。
MAX_VALUATION_COMPARISONS_PER_CLAIM = 3
# 单次 create_claim_batch 最多 3 条 valuation Claim（与 Financial 的
# MAX_FINANCIAL_CLAIMS_PER_BATCH 一致）。
MAX_VALUATION_CLAIMS_PER_BATCH = 3

_RELATIONS = ("supports", "contradicts", "context")


class ValuationClaimAssessment(StrEnum):
    """Relative Valuation Claim 的分析判断（v1；结构化输入，非公式输出）。

    - relative_high：相对 peer 集合整体偏高；
    - broadly_in_line：与 peer 集合基本一致；
    - relative_low：相对 peer 集合整体偏低；
    - mixed：多个 metric 的结论不一致；
    - uncertain：无法给出明确判断。

    **不做** buy / sell / bullish / bearish / cheap / expensive（买卖建议 /
    短期预测边界）。Assessment 是 Analyst 的判断，程序**不得**从 premium 自动
    推导（不写 hidden thresholds）。
    """

    RELATIVE_HIGH = "relative_high"
    BROADLY_IN_LINE = "broadly_in_line"
    RELATIVE_LOW = "relative_low"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


_VALUATION_CLAIM_ASSESSMENTS = frozenset(
    (
        ValuationClaimAssessment.RELATIVE_HIGH,
        ValuationClaimAssessment.BROADLY_IN_LINE,
        ValuationClaimAssessment.RELATIVE_LOW,
        ValuationClaimAssessment.MIXED,
        ValuationClaimAssessment.UNCERTAIN,
    )
)


# v1 冻结的估值 metric_code（deterministic canonical order；与
# app.analysis.valuation.packs 的 V alias 排序一致：pe_ttm → pb_mrq → ps_ttm）。
VALUATION_METRIC_CODES = ("pe_ttm", "pb_mrq", "ps_ttm")
_VALUATION_METRIC_ORDER = {code: i for i, code in enumerate(VALUATION_METRIC_CODES)}

# metric_code → statement 中的中文指标名（v2 statement-scope 渲染用）。
_VALUATION_METRIC_LABELS = {
    "pe_ttm": "市盈率",
    "pb_mrq": "市净率",
    "ps_ttm": "市销率",
}


def _normalize_metric_codes(metric_codes: Iterable[str]) -> tuple[str, ...]:
    """去重 + canonical sort（pe_ttm → pb_mrq → ps_ttm）。

    metric_codes 来自真实 verified Comparisons（Service 传入），**不是模型输出**；
    未知 metric / 空集合 → 稳定错误（renderer 无法确定 statement scope）。
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for code in metric_codes:
        if code not in _VALUATION_METRIC_ORDER:
            raise ValuationClaimDraftError(f"不支持的 metric_code: {code}")
        if code not in seen:
            seen.add(code)
            ordered.append(code)
    if not ordered:
        raise ValuationClaimDraftError("statement 渲染需要至少一个 metric_code")
    ordered.sort(key=_VALUATION_METRIC_ORDER.__getitem__)
    return tuple(ordered)


# single-metric（PE / PB / PS 之一）→ assessment → 固定 statement（无 mixed：
# single metric 不可能合法 mixed，见 render 函数）。
_SINGLE_METRIC_STATEMENTS: dict[str, dict[ValuationClaimAssessment, str]] = {}
for _metric, _label in _VALUATION_METRIC_LABELS.items():
    _SINGLE_METRIC_STATEMENTS[_metric] = {
        ValuationClaimAssessment.RELATIVE_HIGH: (
            f"基于{_label}比较，公司当前估值水平高于所选可比公司整体水平。"
        ),
        ValuationClaimAssessment.BROADLY_IN_LINE: (
            f"基于{_label}比较，公司当前估值水平与所选可比公司整体大致相当。"
        ),
        ValuationClaimAssessment.RELATIVE_LOW: (
            f"基于{_label}比较，公司当前估值水平低于所选可比公司整体水平。"
        ),
        ValuationClaimAssessment.UNCERTAIN: (f"现有{_label}比较不足以形成明确的相对估值判断。"),
    }

# multiple-metrics（PE / PB / PS 综合）→ assessment → 固定 statement。
_MULTI_METRIC_STATEMENTS: dict[ValuationClaimAssessment, str] = {
    ValuationClaimAssessment.RELATIVE_HIGH: (
        "基于所选估值指标综合比较，公司当前相对估值水平高于所选可比公司整体水平。"
    ),
    ValuationClaimAssessment.BROADLY_IN_LINE: (
        "基于所选估值指标综合比较，公司当前相对估值水平与所选可比公司整体大致相当。"
    ),
    ValuationClaimAssessment.RELATIVE_LOW: (
        "基于所选估值指标综合比较，公司当前相对估值水平低于所选可比公司整体水平。"
    ),
    ValuationClaimAssessment.MIXED: "不同估值指标对公司的相对估值判断存在分化。",
    ValuationClaimAssessment.UNCERTAIN: "现有估值指标比较不足以形成明确的方向性判断。",
}


def render_valuation_claim_statement(
    assessment: ValuationClaimAssessment,
    metric_codes: Iterable[str],
) -> str:
    """v2 确定性 Relative Valuation Claim statement 渲染（deterministic，LLM 不生成）。

    metric_codes 来自真实 verified Comparisons（Service 传入，**不是模型输出**），
    按 metric 数量区分 statement scope：

    - **single metric**（只用了 PE / PB / PS 之一）：按指标名渲染
      （"基于市盈率/市净率/市销率比较……"）。single metric 不可能合法 mixed（现有
      mixed policy 要求 support 中正负方向都有，至少 2 个 support）——若发生 →
      稳定 policy error（ValuationClaimDraftError）。
    - **multiple metrics**（PE / PB / PS 综合）：渲染"基于所选估值指标综合比较……"
      的 multi 文本。

    statement 不含任何数字 / 百分比 / 阈值，也不带 company / peer 名称插值
    （避免把模型输出或未审计文本引入 Claim）。v1 = historical pre-final（无
    metric-scope 区分）；v2 = current statement-scope-safe version；历史 v1 Claim
    **不修改 / 不 backfill**。
    """
    codes = _normalize_metric_codes(metric_codes)
    if len(codes) == 1:
        if assessment == ValuationClaimAssessment.MIXED:
            raise ValuationClaimDraftError(
                "single metric comparison 不可能合法 mixed（mixed policy 要求 "
                "support 中正负方向都有）"
            )
        try:
            return _SINGLE_METRIC_STATEMENTS[codes[0]][assessment]
        except KeyError as exc:
            raise ValuationClaimDraftError(f"不支持 assessment: {assessment}") from exc
    try:
        return _MULTI_METRIC_STATEMENTS[assessment]
    except KeyError as exc:
        raise ValuationClaimDraftError(f"不支持 assessment: {assessment}") from exc


class ValuationClaimConfidence(StrEnum):
    """Valuation Claim 的整体置信度（与 Claim 语义一致，独立枚举防耦合）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ValuationClaimImportance(StrEnum):
    """Valuation Claim 的重要性（normal / critical）。

    critical 要求：每个 support Comparison 的 target Observation + 全部 peer
    Observations 的 source Evidence **全部**满足
    critical_claim_eligible_snapshot=true（Service 校验）。
    """

    NORMAL = "normal"
    CRITICAL = "critical"


def _normalize_comparison_ids(comparison_ids: list[UUID]) -> list[UUID]:
    """comparison id 列表去重后按 canonical 顺序排序（fingerprint / replay 全确定性）。"""
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in comparison_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise ValuationClaimDraftError("comparison ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


def _normalize_evidence_ids(evidence_ids: list[UUID]) -> list[UUID]:
    """additional Evidence id 列表去重后按 canonical 顺序排序（fingerprint / replay 全确定性）。

    - 输入必须是 list[UUID]（构造时校验）；
    - 去重（保持首次出现）后再按 str(uuid) 升序排序 → 与调用方提交顺序无关，
      fingerprint / replay 全确定性；
    - 任一 id 非 UUID → ValuationClaimDraftError。
    """
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in evidence_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise ValuationClaimDraftError("additional evidence ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class ValuationClaimDraft:
    """调用方提交的 Relative Valuation Claim 语义输入（构造时校验，不可变）。

    只允许提供语义输入；Comparison replay integrity / 自动 Evidence expansion /
    critical policy / fingerprint / created_at 一律由 ValuationClaimService 从
    真实数据确定性派生，调用方**不得**手工伪造 derived Evidence IDs。

    - research_question / statement / analyst_name：trim 后非空；
    - analysis_as_of：判断对应的研究时点（date）；与 selected comparisons 的
      comparison.analysis_as_of **必须完全一致**（Service 校验，不自动对齐）；
    - assessment：ValuationClaimAssessment——**分析判断**，不是程序从 premium
      自动推导的公式输出；程序不写 hidden thresholds；
    - confidence / importance：对应枚举（critical 需全部 support-comparison
      source Evidence 满足 critical_claim_eligible_snapshot=true）；
    - support_comparison_ids / contradict_comparison_ids / context_comparison_ids：
      去重 + canonical 排序；**至少 1 个 support_comparison_id**；同一 comparison
      不能跨 relation 重复；**v1 最多 3 个 comparison**（PE / PB / PS 各最多 1 个，
      metric 唯一性由 Service 对真实 Comparison 校验）；
    - additional_support_evidence_ids / additional_contradict_evidence_ids /
      additional_context_evidence_ids：去重 + canonical 排序；同一 Evidence 不能
      跨 relation 重复；与自动展开的 source Evidence（relation=context）冲突 →
      Service 抛 ValuationClaimRelationConflict；
    - analyst_version >= 1；analyst_model_id 可选（trim，空串 → None）。
    """

    company_id: UUID
    research_question: str
    analysis_as_of: date
    statement: str
    assessment: ValuationClaimAssessment
    confidence: ValuationClaimConfidence
    importance: ValuationClaimImportance
    support_comparison_ids: list[UUID]
    contradict_comparison_ids: list[UUID]
    context_comparison_ids: list[UUID]
    additional_support_evidence_ids: list[UUID]
    additional_contradict_evidence_ids: list[UUID]
    additional_context_evidence_ids: list[UUID]
    analyst_name: str
    analyst_version: int
    analyst_model_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise ValuationClaimDraftError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise ValuationClaimDraftError("research_question 不能为空（trim 后）")
        statement = self.statement.strip()
        if not statement:
            raise ValuationClaimDraftError("statement 不能为空（trim 后）")
        if isinstance(self.analysis_as_of, bool) or not isinstance(self.analysis_as_of, date):
            raise ValuationClaimDraftError("analysis_as_of 必须是 date")
        if not isinstance(self.assessment, ValuationClaimAssessment):
            raise ValuationClaimDraftError("assessment 必须是 ValuationClaimAssessment")
        if self.assessment not in _VALUATION_CLAIM_ASSESSMENTS:
            raise ValuationClaimDraftError("不支持的 assessment")
        if not isinstance(self.confidence, ValuationClaimConfidence):
            raise ValuationClaimDraftError("confidence 必须是 ValuationClaimConfidence")
        if not isinstance(self.importance, ValuationClaimImportance):
            raise ValuationClaimDraftError("importance 必须是 ValuationClaimImportance")
        name = self.analyst_name.strip()
        if not name:
            raise ValuationClaimDraftError("analyst_name 不能为空（trim 后）")
        if (
            isinstance(self.analyst_version, bool)
            or not isinstance(self.analyst_version, int)
            or self.analyst_version < 1
        ):
            raise ValuationClaimDraftError("analyst_version 必须 >= 1")
        model_id = self.analyst_model_id
        if model_id is not None:
            model_id = model_id.strip()
            if not model_id:
                model_id = None

        supports = _normalize_comparison_ids(self.support_comparison_ids)
        contradicts = _normalize_comparison_ids(self.contradict_comparison_ids)
        context = _normalize_comparison_ids(self.context_comparison_ids)
        all_comp_ids = set(supports) | set(contradicts) | set(context)
        if len(all_comp_ids) != len(supports) + len(contradicts) + len(context):
            raise ValuationClaimDraftError("同一 comparison 不能跨 relation 重复")
        if not supports:
            raise ValuationClaimDraftError("valuation claim 至少需要 1 个 support_comparison_id")
        if len(all_comp_ids) > MAX_VALUATION_COMPARISONS_PER_CLAIM:
            raise ValuationClaimDraftError(
                f"valuation claim v1 最多 {MAX_VALUATION_COMPARISONS_PER_CLAIM} 个 comparison"
                "（PE / PB / PS 各最多 1 个）"
            )

        add_supports = _normalize_evidence_ids(self.additional_support_evidence_ids)
        add_contradicts = _normalize_evidence_ids(self.additional_contradict_evidence_ids)
        add_context = _normalize_evidence_ids(self.additional_context_evidence_ids)
        all_add_ids = set(add_supports) | set(add_contradicts) | set(add_context)
        if len(all_add_ids) != len(add_supports) + len(add_contradicts) + len(add_context):
            raise ValuationClaimDraftError("同一 additional Evidence 不能跨 relation 重复")

        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "analyst_name", name)
        object.__setattr__(self, "analyst_model_id", model_id)
        object.__setattr__(self, "support_comparison_ids", supports)
        object.__setattr__(self, "contradict_comparison_ids", contradicts)
        object.__setattr__(self, "context_comparison_ids", context)
        object.__setattr__(self, "additional_support_evidence_ids", add_supports)
        object.__setattr__(self, "additional_contradict_evidence_ids", add_contradicts)
        object.__setattr__(self, "additional_context_evidence_ids", add_context)


@dataclass(frozen=True)
class ValuationClaimResult:
    """一次 create_claim 的结果摘要（不含任何正文文本 / 数值细节）。"""

    claim_id: UUID
    claim_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class ValuationClaimBatchItem:
    """batch 中单个 draft 的结果（ordinal 从 1 开始，与 input drafts 一一对应）。

    - ordinal：draft 在本次 batch 中的位置（1..len(drafts)）；
    - claim_id：created 或 replayed 后的 Claim id；
    - replayed：True=复用既有 fingerprint 的 Claim，False=本次真正新增。
    """

    ordinal: int
    claim_id: UUID
    replayed: bool


@dataclass(frozen=True)
class ValuationClaimBatchResult:
    """一次 create_claim_batch 的结果摘要（不含任何正文文本 / evidence）。

    - items：**ordered result**——按 input drafts 顺序的逐条结果，
      len(items) == len(drafts)，items[i] 永远对应 drafts[i]（不按
      created/replayed 分组重排）；
    - fingerprints：claim_id → claim_fingerprint（供上游追溯）；
    - claim_ids / created / replayed / created_count / replayed_count：由
      items 顺序派生（不是各自分组拼接）。
    """

    items: tuple[ValuationClaimBatchItem, ...]
    fingerprints: dict[UUID, str]

    @property
    def claim_ids(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items)

    @property
    def created(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items if not item.replayed)

    @property
    def replayed(self) -> tuple[UUID, ...]:
        return tuple(item.claim_id for item in self.items if item.replayed)

    @property
    def created_count(self) -> int:
        return len(self.created)

    @property
    def replayed_count(self) -> int:
        return len(self.replayed)


def compute_valuation_claim_fingerprint(
    *,
    claim_schema_version: int,
    profile_schema_version: int,
    company_id: UUID,
    research_question: str,
    analysis_as_of: date,
    statement: str,
    assessment: str,
    confidence: str,
    importance: str,
    analyst_name: str,
    analyst_version: int,
    analyst_model_id: str | None,
    supports_evidence: list[dict],
    contradicts_evidence: list[dict],
    context_evidence: list[dict],
    supports_comparisons: list[dict],
    contradicts_comparisons: list[dict],
    context_comparisons: list[dict],
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    **至少包含**（spec R）：
    - claim_schema_version=7、profile_schema_version=1；
    - company_id / research_question / analysis_as_of；
    - statement / assessment / confidence / importance；
    - analyst_name / analyst_version / analyst_model_id；
    - 按 relation 分组的 comparison 组（supports / contradicts / context，
      每 entry 含 comparison_id + comparison_fingerprint）；
    - 按 relation 分组的 evidence 组（supports / contradicts / context，每
      entry 含 evidence_card_id + evidence_fingerprint；**含自动展开的 source
      Evidence 与 additional Evidence**）。

    **不得包含** claim_id / created_at。同一完全相同 valuation Claim → 同一
    指纹 → replay 同一行；任一变化 → 新指纹 → 新 Claim，旧行保留（修改 = 新
    Claim，无 update API）。
    """
    payload = {
        "claim_schema_version": claim_schema_version,
        "profile_schema_version": profile_schema_version,
        "company_id": str(company_id),
        "research_question": research_question,
        "analysis_as_of": analysis_as_of.isoformat(),
        "statement": statement,
        "analysis_domain": "valuation",
        "claim_kind": "relative_valuation",
        "assessment": assessment,
        "confidence": confidence,
        "importance": importance,
        "analyst_name": analyst_name,
        "analyst_version": analyst_version,
        "analyst_model_id": analyst_model_id,
        "supports": supports_evidence,
        "contradicts": contradicts_evidence,
        "context": context_evidence,
        "supports_comparisons": supports_comparisons,
        "contradicts_comparisons": contradicts_comparisons,
        "context_comparisons": context_comparisons,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
