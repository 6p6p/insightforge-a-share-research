"""Claim synthesis input contracts (stage 4D.1A): draft + verified claim + summary + fingerprint.

角色边界（Synthesis = 综合阶段的输入集边界，不是 Report / DraftSection）：
- **输入选择是显式的**：调用方 / 未来 LangGraph state 提供 claim_ids
  （2..50 条），本阶段只登记输入集与 provenance 边界，不做语义筛选；
- caller **只能**提供 company_id / research_question / analysis_as_of /
  claim_ids；claim 的 domain / kind / confidence / importance / fingerprint /
  evidence / source 一律从真实 Claims 与 domain provenance 派生，**不得**
  由 caller 提供；
- 每个输入 Claim 必须与 synthesis 的 company / research_question 一致，且其
  Evidence 在 synthesis analysis_as_of 之前可用（no-lookahead）。

synthesis_fingerprint = canonical JSON + SHA-256（sort_keys + 固定 separators
+ UTF-8）：含 synthesis_schema_version / company_id / research_question /
research_question_sha256 / analysis_as_of / claims（按 claim_id canonical
排序，每项 claim_id / claim_fingerprint / analysis_domain / claim_kind /
claim_schema_version）。**不含** synthesis_id / created_at。question / cutoff /
claim set / fingerprint 任一变化 → 新 fingerprint → 新 SynthesisRun；input
提交顺序不影响指纹。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimImportance,
    ClaimKind,
)
from app.synthesis.errors import SynthesisDraftError

# claim_synthesis_runs.synthesis_schema_version 的当前值（改名或换结构时递增；
# 已有 run 的 synthesis_schema_version 原样保留，新语义 → 新 fingerprint）。
CLAIM_SYNTHESIS_SCHEMA_VERSION = 1

# 一次 synthesis 的输入 Claim 数量边界（spec I：2..50，2 以上才有综合意义）。
MIN_SYNTHESIS_CLAIMS = 2
MAX_SYNTHESIS_CLAIMS = 50


def _normalize_claim_ids(claim_ids: list[UUID]) -> list[UUID]:
    """去重后按 canonical 顺序（str(uuid) 升序）排序。

    - 输入必须是 list[UUID]（构造时校验）；
    - 去重（保持首次出现）后再按 str(uuid) 升序排序 → 与调用方提交顺序无关，
      fingerprint / replay 全确定性；
    - 任一 id 非 UUID → SynthesisDraftError。
    """
    seen: set[UUID] = set()
    ordered: list[UUID] = []
    for item in claim_ids:
        if isinstance(item, bool) or not isinstance(item, UUID):
            raise SynthesisDraftError("claim_ids 必须是 UUID")
        if item not in seen:
            seen.add(item)
            ordered.append(item)
    if not (MIN_SYNTHESIS_CLAIMS <= len(ordered) <= MAX_SYNTHESIS_CLAIMS):
        raise SynthesisDraftError(
            f"claim_ids 必须在 {MIN_SYNTHESIS_CLAIMS}..{MAX_SYNTHESIS_CLAIMS} 条"
        )
    return sorted(ordered, key=str)


@dataclass(frozen=True)
class SynthesisInputDraft:
    """调用方提交的 Claim Synthesis 语义输入（构造时校验，不可变）。

    只允许提供语义输入（company_id / research_question / analysis_as_of /
    claim_ids）；claim 的 domain / kind / confidence / importance / fingerprint /
    evidence / source / company / question 一致性一律由 SynthesisService 从真实
    Claims 与 domain provenance 确定性校验派生，调用方**不得**提供。

    - research_question：trim 后非空；
    - analysis_as_of：必填 date（synthesis 综合 cutoff，no-lookahead）；
    - claim_ids：2..50 条，去重 + canonical 排序（deterministic order）。
    """

    company_id: UUID
    research_question: str
    analysis_as_of: date
    claim_ids: list[UUID]

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise SynthesisDraftError("company_id 必须是 UUID")
        question = self.research_question.strip()
        if not question:
            raise SynthesisDraftError("research_question 不能为空（trim 后）")
        if isinstance(self.analysis_as_of, bool) or not isinstance(self.analysis_as_of, date):
            raise SynthesisDraftError("analysis_as_of 必须是 date")
        claim_ids = _normalize_claim_ids(self.claim_ids)
        object.__setattr__(self, "research_question", question)
        object.__setattr__(self, "claim_ids", claim_ids)


@dataclass(frozen=True)
class VerifiedSynthesisClaim:
    """经 ClaimIntegrityGateway 完整性校验后的输入 Claim 投影（不可变）。

    - domain_analysis_as_of：macro = macro_transmission_chains.analysis_as_of
      （legacy v1/v2 链无该列 → 不受支持）；valuation =
      relative_valuation_claim_profiles.analysis_as_of；其余 domain = None。
    - evidence_card_ids：claim_evidence_links 全部 card ids（canonical 排序），
      供 temporal 校验（evidence availability <= synthesis cutoff）。
    """

    claim_id: UUID
    claim_fingerprint: str
    company_id: UUID
    research_question_sha256: str
    analysis_domain: ClaimAnalysisDomain
    claim_kind: ClaimKind
    statement: str
    confidence: ClaimConfidence
    importance: ClaimImportance
    claim_schema_version: int
    analyst_name: str
    analyst_version: int
    analyst_model_id: str | None
    evidence_card_ids: list[UUID]
    domain_analysis_as_of: date | None = None


@dataclass(frozen=True)
class VerifiedSynthesisRun:
    """经 read-side integrity 校验的 SynthesisRun 投影（不可变）。

    `SynthesisService.verify_synthesis_integrity` 的输出：重新加载 run + input
    links → gateway 校验全部 Claims → 以 run 自身字段为预期重跑 company /
    research-question / temporal 政策 → 重算 synthesis_fingerprint 并与
    persisted 比较 → 任一损坏即抛错（**不自动 repair**）。消费方（LangGraph
    合成节点 / SynthesisAnalysisService）只消费 VerifiedSynthesisRun，**不
    重新实现** SynthesisRun replay 规则。

    - verified_claims：全部经 gateway + 隔离 + temporal 校验的 Claim，按
      claim_id canonical 排序（与 fingerprint 计算顺序一致）。
    """

    synthesis_id: UUID
    company_id: UUID
    research_question: str
    research_question_sha256: str
    analysis_as_of: date
    synthesis_fingerprint: str
    verified_claims: list[VerifiedSynthesisClaim]


@dataclass(frozen=True)
class SynthesisInputSummary:
    """一次 synthesis 输入的确定性结构摘要（纯函数派生，无 DB）。

    本阶段**不决定** core / conflict / evidence gap（那是合成节点判断），只
    提供结构化输入画像供 LangGraph 消费。
    """

    claim_count: int
    domain_counts: dict[str, int]
    claim_kind_counts: dict[str, int]
    confidence_counts: dict[str, int]
    importance_counts: dict[str, int]


def build_synthesis_input_summary(
    claims: list[VerifiedSynthesisClaim],
) -> SynthesisInputSummary:
    """纯函数：按固定 key 集合统计各维度计数（key 缺失补 0，全确定性）。"""

    def _counts(getter) -> dict[str, int]:
        counts: dict[str, int] = {}
        for claim in claims:
            key = getter(claim)
            counts[key] = counts.get(key, 0) + 1
        return counts

    return SynthesisInputSummary(
        claim_count=len(claims),
        domain_counts=_counts(lambda c: c.analysis_domain.value),
        claim_kind_counts=_counts(lambda c: c.claim_kind.value),
        confidence_counts=_counts(lambda c: c.confidence.value),
        importance_counts=_counts(lambda c: c.importance.value),
    )


def compute_synthesis_fingerprint(
    *,
    synthesis_schema_version: int,
    company_id: UUID,
    research_question: str,
    research_question_sha256: str,
    analysis_as_of: date,
    claims: list[VerifiedSynthesisClaim],
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：synthesis_schema_version、company_id、research_question、
    research_question_sha256、analysis_as_of、claims（按 claim_id canonical
    排序，每项 claim_id / claim_fingerprint / analysis_domain / claim_kind /
    claim_schema_version）。

    **不得包含** synthesis_id / created_at。同一完全相同 input → 同一指纹 →
    replay 同一 run；question / cutoff / claim set / fingerprint 任一变化 →
    新指纹 → 新 run，旧行保留（无 update API）。input 提交顺序不影响指纹。
    """
    payload = {
        "synthesis_schema_version": synthesis_schema_version,
        "company_id": str(company_id),
        "research_question": research_question,
        "research_question_sha256": research_question_sha256,
        "analysis_as_of": analysis_as_of.isoformat(),
        "claims": [
            {
                "claim_id": str(claim.claim_id),
                "claim_fingerprint": claim.claim_fingerprint,
                "analysis_domain": claim.analysis_domain.value,
                "claim_kind": claim.claim_kind.value,
                "claim_schema_version": claim.claim_schema_version,
            }
            for claim in sorted(claims, key=lambda c: str(c.claim_id))
        ],
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
