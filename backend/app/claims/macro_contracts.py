"""Macro Claim contracts (stage 4C.1A): schema v4 + MacroClaimDraft + fingerprints.

4C.1A 持久化 **Macro Evidence + Company Exposure Evidence → Macro Transmission
Chain → Macro Claim** 的宏观传导分析产物。**Transmission 不是 EvidenceCard**：
宏观察（rate changes / FX / PMI / commodity / macro observations）是 source-backed
Macro Evidence；公司暴露（海外收入 / 进口原料依赖 / 债务结构 / 融资需求 /
需求区域 / 供应链暴露）是 source-backed 公司事实；传导链（利率 → financing
channel → 公司有息负债 → 融资成本压力）是 **analysis artifact**，禁止伪装成
EvidenceCard。

本模块冻结：
- `MACRO_CLAIM_SCHEMA_VERSION = 4`——Macro Claim 的 claim_schema_version
  （analysis_domain=macro）。**不污染 generic ClaimDraft / FinancialClaimDraft**。
- `MACRO_TRANSMISSION_SCHEMA_VERSION = 1`——传导链 schema。
- `MacroClaimDraft`：专用新类。claim_kind **只允许 inference / risk**（macro
  facts 由 Macro Evidence 承载；不做 fact / relative_valuation）。固定
  analysis_domain=macro。至少 1 个 macro_driver_evidence_id + 1 个
  company_exposure_evidence_id；observed_effect_evidence_ids ≥0。
- 证据约束：同一 Evidence 不能出现在任何两个传导角色；同一 Evidence 不能同时
  出现在传导角色与 additional relation 组；additional 三组 relation 互相排斥。
- transmission fingerprint = canonical JSON + SHA-256（transmission_schema_version /
  company / channel_type / effect_direction / impact_status / time_alignment /
  analysis_as_of / role-sorted evidence_card_id + evidence fingerprint；不含
  transmission_id / created_at / claim_id）。任何变化 → 新 fingerprint → 新链，
  旧链保留。
- macro claim fingerprint = claim_schema_version=4 + company / research_question /
  analysis_as_of / statement / claim_kind / confidence / importance / analyst
  身份 / transmission_fingerprint / additional evidence 按 supports/contradicts/
  context 分组（不含 claim_id / created_at）。任何变化 → 新指纹 → 新 Claim + 新
  transmission，旧对象保留。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from app.claims.contracts import ClaimKind
from app.claims.macro_errors import MacroClaimDraftError

# claims.claim_schema_version 的当前值（analysis_domain=macro 专用）。
MACRO_CLAIM_SCHEMA_VERSION = 4
# macro_transmission_chains.transmission_schema_version 的当前值。
MACRO_TRANSMISSION_SCHEMA_VERSION = 1

# Macro Claim 只允许 inference / risk（macro facts 由 Macro Evidence 承载）。
_MACRO_CLAIM_KINDS = frozenset((ClaimKind.INFERENCE, ClaimKind.RISK))
_RELATIONS = ("supports", "contradicts", "context")


class MacroClaimConfidence(StrEnum):
    """Macro Claim 的整体置信度（与 Claim 语义一致，独立枚举防耦合）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MacroClaimImportance(StrEnum):
    """Macro Claim 的重要性（normal / critical；critical 需 eligible 传导双腿）。"""

    NORMAL = "normal"
    CRITICAL = "critical"


class MacroChannelType(StrEnum):
    """宏观如何传到公司（channel 描述传导渠道，不是宏观变量本身）。

    利率 / 汇率 / PMI / 大宗价格是宏观变量（由 Macro Evidence 承载）；channel
    描述公司受到影响的业务/财务途径。
    """

    REVENUE = "revenue"
    COST = "cost"
    FINANCING = "financing"
    DEMAND = "demand"
    SUPPLY_CHAIN = "supply_chain"
    TRADE_POLICY = "trade_policy"
    OPERATIONS = "operations"
    OTHER = "other"


class MacroEffectDirection(StrEnum):
    """宏观传导对公司的净影响方向（**不是 buy/sell/bullish/bearish**）。"""

    TAILWIND = "tailwind"
    HEADWIND = "headwind"
    MIXED = "mixed"
    UNCERTAIN = "uncertain"


class MacroImpactStatus(StrEnum):
    """影响状态：plausible=存在合理传导可能（未声称已发生）；observed=影响已
    被公司证据观察到（需要 observed_effect Evidence）。"""

    PLAUSIBLE_IMPACT = "plausible_impact"
    OBSERVED_IMPACT = "observed_impact"


class MacroTimeAlignment(StrEnum):
    """分析者的时间对应判断（结构化分析字段，**不由程序从日期差自动猜测**）。

    无 misaligned：证据明确错位时 Service 拒绝创建而非保存 misaligned Claim。
    """

    ALIGNED = "aligned"
    UNCERTAIN = "uncertain"


class MacroTransmissionRole(StrEnum):
    """传导链中 EvidenceCard 的角色（存于 macro_transmission_evidence_links）。"""

    MACRO_DRIVER = "macro_driver"
    COMPANY_EXPOSURE = "company_exposure"
    OBSERVED_EFFECT = "observed_effect"


def _normalize_ids(evidence_ids: list[UUID]) -> list[UUID]:
    """evidence id 列表去重后按 canonical 顺序排序（fingerprint / replay 全确定性）。"""
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in evidence_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise MacroClaimDraftError("evidence ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class MacroClaimDraft:
    """调用方提交的 Macro Claim 语义输入（构造时校验，不可变）。

    只允许提供语义输入；company 一致性 / origin 校验 / 可用时间 / temporal
    policy / critical policy / impact-status rule / fingerprint / created_at 一律
    由 MacroClaimService 从真实数据确定性派生，调用方**不得**手工伪造
    derived Evidence IDs / provenance。

    - research_question / statement / analyst_name：trim 后非空；
    - analysis_as_of：**必填 date**（分析基准日，用于时间对应校验）；
    - claim_kind 只能是 inference / risk（macro facts 由 Macro Evidence 承载；
      不做 fact / relative_valuation）；
    - macro_driver_evidence_ids ≥1、company_exposure_evidence_ids ≥1、
      observed_effect_evidence_ids ≥0；
    - 同一 Evidence 不能出现在任何两个传导角色；同一 Evidence 不能同时出现在
      传导角色与 additional relation 组；additional 三组 relation 互相排斥；
    - confidence / importance / channel_type / effect_direction / impact_status /
      time_alignment：对应 StrEnum；analyst_version >= 1；analyst_model_id
      可选（trim，空串 → None）。
    """

    company_id: UUID
    research_question: str
    analysis_as_of: date
    statement: str
    claim_kind: ClaimKind
    confidence: MacroClaimConfidence
    importance: MacroClaimImportance
    channel_type: MacroChannelType
    effect_direction: MacroEffectDirection
    impact_status: MacroImpactStatus
    time_alignment: MacroTimeAlignment
    macro_driver_evidence_ids: list[UUID]
    company_exposure_evidence_ids: list[UUID]
    observed_effect_evidence_ids: list[UUID]
    additional_support_evidence_ids: list[UUID]
    additional_contradict_evidence_ids: list[UUID]
    additional_context_evidence_ids: list[UUID]
    analyst_name: str
    analyst_version: int
    analyst_model_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise MacroClaimDraftError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise MacroClaimDraftError("research_question 不能为空（trim 后）")
        statement = self.statement.strip()
        if not statement:
            raise MacroClaimDraftError("statement 不能为空（trim 后）")
        if isinstance(self.analysis_as_of, bool) or not isinstance(self.analysis_as_of, date):
            raise MacroClaimDraftError("analysis_as_of 必须是 date")
        if not isinstance(self.claim_kind, ClaimKind):
            raise MacroClaimDraftError("claim_kind 必须是 ClaimKind")
        if self.claim_kind not in _MACRO_CLAIM_KINDS:
            raise MacroClaimDraftError(
                "macro claim_kind 只能是 inference / risk（macro facts 由 Macro Evidence 承载）"
            )
        if not isinstance(self.confidence, MacroClaimConfidence):
            raise MacroClaimDraftError("confidence 必须是 MacroClaimConfidence")
        if not isinstance(self.importance, MacroClaimImportance):
            raise MacroClaimDraftError("importance 必须是 MacroClaimImportance")
        if not isinstance(self.channel_type, MacroChannelType):
            raise MacroClaimDraftError("channel_type 必须是 MacroChannelType")
        if not isinstance(self.effect_direction, MacroEffectDirection):
            raise MacroClaimDraftError("effect_direction 必须是 MacroEffectDirection")
        if not isinstance(self.impact_status, MacroImpactStatus):
            raise MacroClaimDraftError("impact_status 必须是 MacroImpactStatus")
        if not isinstance(self.time_alignment, MacroTimeAlignment):
            raise MacroClaimDraftError("time_alignment 必须是 MacroTimeAlignment")
        name = self.analyst_name.strip()
        if not name:
            raise MacroClaimDraftError("analyst_name 不能为空（trim 后）")
        if (
            isinstance(self.analyst_version, bool)
            or not isinstance(self.analyst_version, int)
            or self.analyst_version < 1
        ):
            raise MacroClaimDraftError("analyst_version 必须 >= 1")
        model_id = self.analyst_model_id
        if model_id is not None:
            model_id = model_id.strip()
            if not model_id:
                model_id = None

        macro_drivers = _normalize_ids(self.macro_driver_evidence_ids)
        exposures = _normalize_ids(self.company_exposure_evidence_ids)
        observed = _normalize_ids(self.observed_effect_evidence_ids)
        add_supports = _normalize_ids(self.additional_support_evidence_ids)
        add_contradicts = _normalize_ids(self.additional_contradict_evidence_ids)
        add_context = _normalize_ids(self.additional_context_evidence_ids)

        if not macro_drivers:
            raise MacroClaimDraftError("macro claim 至少需要 1 个 macro_driver_evidence_id")
        if not exposures:
            raise MacroClaimDraftError("macro claim 至少需要 1 个 company_exposure_evidence_id")

        # 同一 Evidence 不能出现在任何两个传导角色。
        transmission_ids = set(macro_drivers) | set(exposures) | set(observed)
        if len(transmission_ids) != len(macro_drivers) + len(exposures) + len(observed):
            raise MacroClaimDraftError("同一 Evidence 不能出现在多个传导角色")

        # additional 三组 relation 互相排斥。
        add_ids = set(add_supports) | set(add_contradicts) | set(add_context)
        if len(add_ids) != len(add_supports) + len(add_contradicts) + len(add_context):
            raise MacroClaimDraftError("同一 additional Evidence 不能跨 relation 重复")

        # 同一 Evidence 不能同时出现在传导角色与 additional relation 组。
        if transmission_ids & add_ids:
            raise MacroClaimDraftError(
                "同一 Evidence 不能同时出现在传导角色与 additional relation 组"
            )

        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "analyst_name", name)
        object.__setattr__(self, "analyst_model_id", model_id)
        object.__setattr__(self, "macro_driver_evidence_ids", macro_drivers)
        object.__setattr__(self, "company_exposure_evidence_ids", exposures)
        object.__setattr__(self, "observed_effect_evidence_ids", observed)
        object.__setattr__(self, "additional_support_evidence_ids", add_supports)
        object.__setattr__(self, "additional_contradict_evidence_ids", add_contradicts)
        object.__setattr__(self, "additional_context_evidence_ids", add_context)


def compute_macro_transmission_fingerprint(
    *,
    transmission_schema_version: int,
    company_id: UUID,
    channel_type: str,
    effect_direction: str,
    impact_status: str,
    time_alignment: str,
    analysis_as_of: date,
    macro_driver: list[dict],
    company_exposure: list[dict],
    observed_effect: list[dict],
) -> str:
    """传导链的确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    - 至少覆盖：transmission_schema_version、company_id、channel_type /
      effect_direction / impact_status / time_alignment、analysis_as_of、
      按角色分组的 **role-sorted** evidence_card_id + evidence_fingerprint
      （证据稳定指纹，来自真实 EvidenceCard，不伪造）；
    - **不得包含** transmission_id / created_at / claim_id。

    同一完全相同传导 → 同一指纹 → replay 同一行；语义任一变化 → 新指纹 → 新链，
    旧链保留（无 update API）。
    """
    payload = {
        "transmission_schema_version": transmission_schema_version,
        "company_id": str(company_id),
        "channel_type": channel_type,
        "effect_direction": effect_direction,
        "impact_status": impact_status,
        "time_alignment": time_alignment,
        "analysis_as_of": analysis_as_of.isoformat(),
        "macro_driver": macro_driver,
        "company_exposure": company_exposure,
        "observed_effect": observed_effect,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def compute_macro_claim_fingerprint(
    *,
    claim_schema_version: int,
    company_id: UUID,
    research_question: str,
    analysis_as_of: date,
    statement: str,
    claim_kind: str,
    confidence: str,
    importance: str,
    analyst_name: str,
    analyst_version: int,
    analyst_model_id: str | None,
    transmission_fingerprint: str,
    additional_supports: list[UUID],
    additional_contradicts: list[UUID],
    additional_context: list[UUID],
) -> str:
    """Macro Claim 的确定性 SHA-256 指纹（sort_keys + 固定 separators）。

    - 至少覆盖：claim_schema_version（=4）、company_id、research_question、
      analysis_as_of、statement、analysis_domain=macro、claim_kind、confidence、
      importance、analyst 身份、transmission_fingerprint、additional evidence 按
      supports/contradicts/context 分组的 ordered evidence_card_ids；
    - **不得包含** claim_id / created_at。同一完全相同 Macro Claim → 同一指纹 →
      replay 同一行；任一变化 → 新指纹 → 新 Claim + 新 transmission，旧对象保留。
    """
    payload = {
        "claim_schema_version": claim_schema_version,
        "company_id": str(company_id),
        "research_question": research_question,
        "analysis_as_of": analysis_as_of.isoformat(),
        "statement": statement,
        "analysis_domain": "macro",
        "claim_kind": claim_kind,
        "confidence": confidence,
        "importance": importance,
        "analyst_name": analyst_name,
        "analyst_version": analyst_version,
        "analyst_model_id": analyst_model_id,
        "transmission_fingerprint": transmission_fingerprint,
        "additional_supports": [str(card_id) for card_id in additional_supports],
        "additional_contradicts": [str(card_id) for card_id in additional_contradicts],
        "additional_context": [str(card_id) for card_id in additional_context],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
