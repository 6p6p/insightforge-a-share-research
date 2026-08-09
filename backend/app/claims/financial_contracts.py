"""Financial Claim contracts (stage 4B.2C.1): schema v3 + FinancialClaimDraft + fingerprint.

4B.2C.1 持久化 **Claim ↔ FinancialCalculation** 链接
（Claim → ClaimFinancialCalculationLink → FinancialCalculation →
FinancialMetricObservation → EvidenceCard → Source），使 Audit 可确定性重算。
**不把 FinancialCalculation 伪装成 EvidenceCard**：Calculation = derived
deterministic fact，EvidenceCard = source-backed fact，保持分层。

本模块冻结：
- `FINANCIAL_CLAIM_SCHEMA_VERSION = 3`——**source Evidence relation semantics
  修正**：Calculation 承担 supports/contradicts/context 的 Claim 语义关系，
  underlying source Evidence 自动展开到 ClaimEvidenceLinks 时**一律
  relation=context**（不是 propagation）。不要"support calculation → raw revenue
  Evidence supports Claim"，也不要"contradict calculation → raw cost Evidence
  contradicts Claim"：原始 metric Evidence 通常不能单独证明 derived financial
  conclusion。additional Evidence 保持调用方指定的 supports/contradicts/context。
- `FINANCIAL_CLAIM_SCHEMA_VERSION_V2 = 2`——v2 已落地（原 relation propagation
  + 原 critical policy），**保持其 replay 语义，不静默修改历史 v2 rows、不重新
  计算旧 fingerprint**。已有 v2 Claims 必须继续可读/replay。
- `FinancialClaimDraft`：**专用新类，不污染 ClaimDraft**。语义输入 = company_id /
  research_question / statement / confidence / importance / claim_kind /
  support/contradict/context calculation ids / additional support/contradict/
  context evidence ids / analyst_name / analyst_version / analyst_model_id
  optional / claim_schema_version（2 或 3，默认 3）。**固定 analysis_domain=
  financial**；claim_kind ∈ fact / inference / risk（**不做 relative_valuation**，
  估值留 4C）。至少 1 support_calculation_id。同一 calculation 不能跨 relation
  重复。
- v2/v3 fingerprint 都包含 claim_schema_version + claim semantic fields +
  evidence links + calculation links/fingerprints，因此 **v2/v3 不会错误
  collision**（同 draft 以不同 schema_version 创建 → 不同指纹 → 两行）。
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.claims.contracts import ClaimKind, _normalize_evidence_ids
from app.claims.financial_errors import FinancialClaimDraftError

# claims.claim_schema_version 的当前值（financial Claim；含 Calculation links）。
# v3 修正 source Evidence 语义：Calculation 承担 supports/contradicts/context，
# 自动展开的 source Evidence 一律 relation=context；additional Evidence 保持
# 调用方指定的 relation。
FINANCIAL_CLAIM_SCHEMA_VERSION = 3
# v2 是已落地的 legacy schema（原 relation propagation + 原 critical policy），
# 保持 replay 语义；不修改历史 v2 rows、不重新计算旧 fingerprint。
FINANCIAL_CLAIM_SCHEMA_VERSION_V2 = 2
# financial Claim 支持的 schema 版本集合（禁止其他版本混入）。
_FINANCIAL_CLAIM_SCHEMA_VERSIONS = frozenset(
    (FINANCIAL_CLAIM_SCHEMA_VERSION_V2, FINANCIAL_CLAIM_SCHEMA_VERSION)
)

_FINANCIAL_CLAIM_KINDS = frozenset((ClaimKind.FACT, ClaimKind.INFERENCE, ClaimKind.RISK))
_RELATIONS = ("supports", "contradicts", "context")


class FinancialClaimConfidence(StrEnum):
    """Financial Claim 的整体置信度（与 Claim 语义一致，独立枚举防耦合）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FinancialClaimImportance(StrEnum):
    """Financial Claim 的重要性（normal / critical；critical 需 eligible 支持）。"""

    NORMAL = "normal"
    CRITICAL = "critical"


def _normalize_calculation_ids(calculation_ids: list[UUID]) -> list[UUID]:
    """calculation id 列表去重后按 canonical 顺序排序（fingerprint / replay 全确定性）。"""
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in calculation_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise FinancialClaimDraftError("calculation ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class FinancialClaimDraft:
    """调用方提交的 Financial Claim 语义输入（构造时校验，不可变）。

    只允许提供语义输入；company 一致性 / Calculation replay integrity / 自动
    Evidence expansion / relation propagation / critical policy / fingerprint /
    created_at 一律由 FinancialClaimService 从真实数据确定性派生，调用方
    **不得**手工伪造 derived Evidence IDs。

    - research_question / statement / analyst_name：trim 后非空；
    - confidence / importance / claim_kind：对应枚举；claim_kind 只能是
      fact / inference / risk（不做 relative_valuation）；
    - support_calculation_ids / contradict_calculation_ids /
      context_calculation_ids：去重 + canonical 排序；**至少 1 个
      support_calculation_id**；同一 calculation 不能跨 relation 重复；
    - additional_support_evidence_ids / additional_contradict_evidence_ids /
      additional_context_evidence_ids：**只用于管理层解释 / 业务事件 / 风险
      说明等额外定性 Evidence**（与自动展开的 source Evidence 分开）；去重 +
      canonical 排序；同一 Evidence 不能跨 relation 重复；
    - analyst_version >= 1；analyst_model_id 可选（trim，空串 → None）；
    - claim_schema_version：2 或 3（默认 3）。v3 用新 context propagation +
      新 critical source policy；v2 沿用原 relation propagation + 原 policy
      （仅用于既有 v2 Claim 的 replay 兼容，业务新创建一律 v3）。
    """

    company_id: UUID
    research_question: str
    statement: str
    confidence: FinancialClaimConfidence
    importance: FinancialClaimImportance
    claim_kind: ClaimKind
    support_calculation_ids: list[UUID]
    contradict_calculation_ids: list[UUID]
    context_calculation_ids: list[UUID]
    additional_support_evidence_ids: list[UUID]
    additional_contradict_evidence_ids: list[UUID]
    additional_context_evidence_ids: list[UUID]
    analyst_name: str
    analyst_version: int
    analyst_model_id: str | None = None
    claim_schema_version: int = FINANCIAL_CLAIM_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise FinancialClaimDraftError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise FinancialClaimDraftError("research_question 不能为空（trim 后）")
        statement = self.statement.strip()
        if not statement:
            raise FinancialClaimDraftError("statement 不能为空（trim 后）")
        if not isinstance(self.confidence, FinancialClaimConfidence):
            raise FinancialClaimDraftError("confidence 必须是 FinancialClaimConfidence")
        if not isinstance(self.importance, FinancialClaimImportance):
            raise FinancialClaimDraftError("importance 必须是 FinancialClaimImportance")
        if not isinstance(self.claim_kind, ClaimKind):
            raise FinancialClaimDraftError("claim_kind 必须是 ClaimKind")
        if self.claim_kind not in _FINANCIAL_CLAIM_KINDS:
            raise FinancialClaimDraftError(
                "financial claim_kind 只能是 fact / inference / risk（不做 relative_valuation）"
            )
        name = self.analyst_name.strip()
        if not name:
            raise FinancialClaimDraftError("analyst_name 不能为空（trim 后）")
        if (
            isinstance(self.analyst_version, bool)
            or not isinstance(self.analyst_version, int)
            or self.analyst_version < 1
        ):
            raise FinancialClaimDraftError("analyst_version 必须 >= 1")
        model_id = self.analyst_model_id
        if model_id is not None:
            model_id = model_id.strip()
            if not model_id:
                model_id = None
        if (
            isinstance(self.claim_schema_version, bool)
            or not isinstance(self.claim_schema_version, int)
            or self.claim_schema_version not in _FINANCIAL_CLAIM_SCHEMA_VERSIONS
        ):
            raise FinancialClaimDraftError("claim_schema_version 必须是 2 或 3")

        supports = _normalize_calculation_ids(self.support_calculation_ids)
        contradicts = _normalize_calculation_ids(self.contradict_calculation_ids)
        context = _normalize_calculation_ids(self.context_calculation_ids)

        all_calc_ids = set(supports) | set(contradicts) | set(context)
        if len(all_calc_ids) != len(supports) + len(contradicts) + len(context):
            raise FinancialClaimDraftError("同一 calculation 不能跨 relation 重复")
        if not supports:
            raise FinancialClaimDraftError("financial claim 至少需要 1 个 support_calculation_id")

        add_supports = _normalize_evidence_ids(self.additional_support_evidence_ids)
        add_contradicts = _normalize_evidence_ids(self.additional_contradict_evidence_ids)
        add_context = _normalize_evidence_ids(self.additional_context_evidence_ids)
        all_add_ids = set(add_supports) | set(add_contradicts) | set(add_context)
        if len(all_add_ids) != len(add_supports) + len(add_contradicts) + len(add_context):
            raise FinancialClaimDraftError("同一 additional Evidence 不能跨 relation 重复")

        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "analyst_name", name)
        object.__setattr__(self, "analyst_model_id", model_id)
        object.__setattr__(self, "support_calculation_ids", supports)
        object.__setattr__(self, "contradict_calculation_ids", contradicts)
        object.__setattr__(self, "context_calculation_ids", context)
        object.__setattr__(self, "additional_support_evidence_ids", add_supports)
        object.__setattr__(self, "additional_contradict_evidence_ids", add_contradicts)
        object.__setattr__(self, "additional_context_evidence_ids", add_context)


def compute_financial_claim_fingerprint(
    *,
    claim_schema_version: int,
    company_id: UUID,
    research_question: str,
    statement: str,
    claim_kind: str,
    confidence: str,
    importance: str,
    analyst_name: str,
    analyst_version: int,
    analyst_model_id: str | None,
    supports_evidence: list[UUID],
    contradicts_evidence: list[UUID],
    context_evidence: list[UUID],
    supports_calculations: list[dict],
    contradicts_calculations: list[dict],
    context_calculations: list[dict],
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    = v1 内容（claim_schema_version / company / research_question /
    statement / analysis_domain=financial / claim_kind / confidence /
    importance / analyst 身份 / 按 relation 分组的 ordered evidence_card_ids）+
    **按 relation 排序的 supports_calculations / contradicts_calculations /
    context_calculations**（每 entry 至少 calculation_id +
    calculation_fingerprint）。

    `claim_schema_version` 由调用方传入（2 或 3）：**v2/v3 fingerprint 都包含
    schema version + claim semantic fields + evidence links + calculation
    links/fingerprints**，因此同 draft 以不同 schema_version 派生 → 不同指纹 →
    不会错误 collision。v3 的 evidence_by_relation 因 context propagation 天然
    与 v2 不同（source Evidence 在 context 而非 supports），进一步隔离。

    **不得包含** claim_id / created_at。同一完全相同 Financial Claim → 同一指纹
    → replay 同一行；任一变化 → 新指纹 → 新行，旧行保留（修改 = 新 Claim）。
    """
    payload = {
        "claim_schema_version": claim_schema_version,
        "company_id": str(company_id),
        "research_question": research_question,
        "statement": statement,
        "analysis_domain": "financial",
        "claim_kind": claim_kind,
        "confidence": confidence,
        "importance": importance,
        "analyst_name": analyst_name,
        "analyst_version": analyst_version,
        "analyst_model_id": analyst_model_id,
        "supports": [str(card_id) for card_id in supports_evidence],
        "contradicts": [str(card_id) for card_id in contradicts_evidence],
        "context": [str(card_id) for card_id in context_evidence],
        "supports_calculations": supports_calculations,
        "contradicts_calculations": contradicts_calculations,
        "context_calculations": context_calculations,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
