"""Claim contracts (stage 4A): ClaimDraft + enums + fingerprint.

角色边界（Claim 是 Stage 4 分析结论，Evidence 是 Stage 3 最小证据单元）：
- **EvidenceCard = 来源事实**（"2025年海外收入同比增长31.4%"）；
- **Claim = 分析结论**（"海外业务是公司2025年收入增长的重要驱动因素"）；
- ClaimService **不做语义判断**——只负责结构 / provenance / 来源政策 /
  relation / fingerprint / persistence / replay。语义支持度由 LLM Analyst /
  later Auditor 判断。

本模块冻结：
- CLAIM_SCHEMA_VERSION = 1；analysis_domain（financial/business/event/macro/
  risk/valuation）；claim_kind（fact/inference/risk/relative_valuation；
  **不含** prediction/buy/sell/recommendation/price_target/return_forecast）；
  confidence（low/medium/high）；importance（normal/critical）；
  ClaimEvidenceRelation（supports/contradicts/context）。
- ClaimDraft：调用方**只能**提供语义输入（company_id / research_question /
  statement / analysis_domain / claim_kind / confidence / importance /
  support/contradict/context evidence ids / analyst_name / analyst_version /
  analyst_model_id optional）。**不得**提供 authority tier / provider /
  source IDs / Evidence provenance / fingerprint / created_at——这些由
  ClaimService 从真实 Evidence 确定性派生。
- 每种 evidence id list 去重后按 canonical 顺序（str(uuid) 升序）排序，
  保证 fingerprint 与 replay 全确定性；同一 EvidenceCard 不能跨 relation
  重复（v1 禁止 supports+context / supports+contradicts / contradicts+context
  任意组合）。
- claim_fingerprint = canonical JSON + SHA-256（含 claim_schema_version /
  company / research_question / statement / analysis_domain / claim_kind /
  confidence / importance / analyst 身份 / 按 relation 分组的 ordered
  evidence_card_ids；不含 claim_id / created_at）。同一完全相同 Claim →
  同一指纹 → replay 同一行；任一变化 → 新指纹 → 新行，旧行保留。
"""

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from uuid import UUID

from app.claims.errors import ClaimDraftError

# claims.claim_schema_version 的当前值（改名或换结构时递增；已有 Claim 的
# claim_schema_version 原样保留，新语义 → 新 fingerprint）。
CLAIM_SCHEMA_VERSION = 1

_ANALYSIS_DOMAINS = ("financial", "business", "event", "macro", "risk", "valuation")
_CLAIM_KINDS = ("fact", "inference", "risk", "relative_valuation")
_CONFIDENCE_LEVELS = ("low", "medium", "high")
_IMPORTANCE_LEVELS = ("normal", "critical")
_RELATIONS = ("supports", "contradicts", "context")


class ClaimAnalysisDomain(StrEnum):
    """Claim 的分析领域。

    - financial: 财务数据 / 指标；
    - business: 业务 / 经营；
    - event: 已发生事件；
    - macro: 宏观传导（需要 macro_observation 支持 + 公司 document evidence）；
    - risk: 风险；
    - valuation: 估值。
    """

    FINANCIAL = "financial"
    BUSINESS = "business"
    EVENT = "event"
    MACRO = "macro"
    RISK = "risk"
    VALUATION = "valuation"


class ClaimKind(StrEnum):
    """Claim 的类型（分析结论）。

    **不含** prediction / buy / sell / recommendation / price_target /
    return_forecast——InsightForge 不做短期预测与买卖建议。
    """

    FACT = "fact"
    INFERENCE = "inference"
    RISK = "risk"
    RELATIVE_VALUATION = "relative_valuation"


class ClaimConfidence(StrEnum):
    """Claim 的整体置信度（结构标注，不替代来源政策 / 语义判断）。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClaimImportance(StrEnum):
    """Claim 的重要性（normal / critical）。

    critical Claim 需要 ≥1 supports Evidence 满足
    critical_claim_eligible_snapshot = true（ClaimCriticalEvidenceInsufficient）。
    """

    NORMAL = "normal"
    CRITICAL = "critical"


class ClaimEvidenceRelation(StrEnum):
    """Claim ↔ EvidenceCard 的关系（关系属于 ClaimEvidenceLink）。"""

    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CONTEXT = "context"


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compute_research_question_sha256(research_question: str) -> str:
    """research_question（trim 后）UTF-8 文本的 SHA-256。

    与 EvidenceCard.research_question_sha256 同算法（同一 question 在
    evidence_cards 与 claims 中哈希一致，便于追溯）。
    """
    return _sha256_hex(research_question.strip())


def _normalize_evidence_ids(evidence_ids: list[UUID]) -> list[UUID]:
    """每种 evidence id list 去重后按 canonical 顺序排序。

    - 输入必须是 list[UUID]（构造时校验）；
    - 去重（保持首次出现）后再按 str(uuid) 升序排序 → 与调用方提交顺序无关，
      fingerprint / replay 全确定性；
    - 任一 id 非 UUID → ClaimDraftError。
    """
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in evidence_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise ClaimDraftError("evidence ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class ClaimDraft:
    """调用方提交的 Claim 语义输入（构造时校验，不可变）。

    只允许提供语义输入；company 一致性 / authority tier / provider / source /
    fingerprint / created_at 一律由 ClaimService 从真实 Evidence 确定性派生，
    调用方**不得**提供。

    - research_question / statement / analyst_name：trim 后非空；
    - analysis_domain / claim_kind / confidence / importance：对应 StrEnum；
    - support_evidence_ids / contradict_evidence_ids / context_evidence_ids：
      去重 + canonical 排序（deterministic order）；
    - 同一 EvidenceCard 不能同时出现在多个 relation（v1 跨 relation 重复 →
      ClaimDraftError）；
    - analyst_version >= 1；analyst_model_id 可选（提供时 trim，空串 → None）。
    """

    company_id: UUID
    research_question: str
    statement: str
    analysis_domain: ClaimAnalysisDomain
    claim_kind: ClaimKind
    confidence: ClaimConfidence
    importance: ClaimImportance
    support_evidence_ids: list[UUID]
    contradict_evidence_ids: list[UUID]
    context_evidence_ids: list[UUID]
    analyst_name: str
    analyst_version: int
    analyst_model_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise ClaimDraftError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise ClaimDraftError("research_question 不能为空（trim 后）")
        statement = self.statement.strip()
        if not statement:
            raise ClaimDraftError("statement 不能为空（trim 后）")
        if not isinstance(self.analysis_domain, ClaimAnalysisDomain):
            raise ClaimDraftError("analysis_domain 必须是 ClaimAnalysisDomain")
        if not isinstance(self.claim_kind, ClaimKind):
            raise ClaimDraftError("claim_kind 必须是 ClaimKind")
        if not isinstance(self.confidence, ClaimConfidence):
            raise ClaimDraftError("confidence 必须是 ClaimConfidence")
        if not isinstance(self.importance, ClaimImportance):
            raise ClaimDraftError("importance 必须是 ClaimImportance")
        name = self.analyst_name.strip()
        if not name:
            raise ClaimDraftError("analyst_name 不能为空（trim 后）")
        if (
            isinstance(self.analyst_version, bool)
            or not isinstance(self.analyst_version, int)
            or self.analyst_version < 1
        ):
            raise ClaimDraftError("analyst_version 必须 >= 1")
        model_id = self.analyst_model_id
        if model_id is not None:
            model_id = model_id.strip()
            if not model_id:
                model_id = None

        supports = _normalize_evidence_ids(self.support_evidence_ids)
        contradicts = _normalize_evidence_ids(self.contradict_evidence_ids)
        context = _normalize_evidence_ids(self.context_evidence_ids)

        # v1 禁止同一卡跨 relation 重复（避免 supports+context 语义模糊）。
        all_ids = set(supports) | set(contradicts) | set(context)
        if len(all_ids) != len(supports) + len(contradicts) + len(context):
            raise ClaimDraftError("同一 EvidenceCard 不能跨 relation 重复")

        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "statement", statement)
        object.__setattr__(self, "analyst_name", name)
        object.__setattr__(self, "analyst_model_id", model_id)
        object.__setattr__(self, "support_evidence_ids", supports)
        object.__setattr__(self, "contradict_evidence_ids", contradicts)
        object.__setattr__(self, "context_evidence_ids", context)


def compute_claim_fingerprint(
    *,
    claim_schema_version: int,
    company_id: UUID,
    research_question: str,
    statement: str,
    analysis_domain: str,
    claim_kind: str,
    confidence: str,
    importance: str,
    analyst_name: str,
    analyst_version: int,
    analyst_model_id: str | None,
    supports: list[UUID],
    contradicts: list[UUID],
    context: list[UUID],
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：claim_schema_version、company_id、research_question、statement、
    analysis_domain、claim_kind、confidence、importance、analyst_name /
    analyst_version / analyst_model_id、按 relation 分组的 ordered
    evidence_card_ids（supports / contradicts / context）。

    **不得包含** claim_id / created_at。同一完全相同 Claim → 同一指纹 →
    replay 同一行；statement / evidence relations / confidence / analyst
    version 任一变化 → 新指纹 → 新行，旧行保留（修改观点 = 新 Claim）。
    """
    payload = {
        "claim_schema_version": claim_schema_version,
        "company_id": str(company_id),
        "research_question": research_question,
        "statement": statement,
        "analysis_domain": analysis_domain,
        "claim_kind": claim_kind,
        "confidence": confidence,
        "importance": importance,
        "analyst_name": analyst_name,
        "analyst_version": analyst_version,
        "analyst_model_id": analyst_model_id,
        "supports": [str(card_id) for card_id in supports],
        "contradicts": [str(card_id) for card_id in contradicts],
        "context": [str(card_id) for card_id in context],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
