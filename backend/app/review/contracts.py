"""Report review routing + human confirmation contracts (stage 5E.1).

常量 + 结果投影 + 指纹。角色边界（ReviewRouting 只做确定性派生，不调用 LLM /
不 rewrite / 不 research /
不接 LangGraph，spec A/B）：
- 确定性代码负责：verify Audit → 派生 action_type / action_payload →
  action fingerprint → create_or_get 原子持久化 → human request / decision 派生
  与持久化 → 三层 verify integrity（read-side）；
- 模型 / 人工不参与 action 派生；人工只在 `resolve_human_request` 提供
  decision（approve/rewrite/research/cancel）+ 可选 comment。

冻结常量：
- `REVIEW_ACTION_SCHEMA_VERSION = 1`（report_review_actions.action_schema_version）；
- `HUMAN_REVIEW_REQUEST_SCHEMA_VERSION = 1`（human_review_requests.request_schema_version）；
- `HUMAN_REVIEW_DECISION_SCHEMA_VERSION = 1`（human_review_decisions.decision_schema_version）。
  0 model identity（spec D）。

指纹（canonical JSON + SHA-256，**均不含 row id / created_at / decided_at**）：
- `compute_action_fingerprint` = action schema version / audit_id /
  audit_fingerprint / report_id / report_fingerprint / action_type /
  normalized action_payload；
- `compute_request_fingerprint` = request schema version / review_action_id /
  action_fingerprint / normalized request_payload；
- `compute_decision_fingerprint` = decision schema version / human_request_id /
  request_fingerprint / decision / normalized comment。
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.audit.contracts import VerifiedReportAudit

# report_review_actions.action_schema_version 的当前值（改名或换结构时递增；已有
# action 原样保留，新语义 → 新 action_fingerprint → 新行）。
REVIEW_ACTION_SCHEMA_VERSION = 1

# human_review_requests.request_schema_version 的当前值。
HUMAN_REVIEW_REQUEST_SCHEMA_VERSION = 1

# human_review_decisions.decision_schema_version 的当前值。
HUMAN_REVIEW_DECISION_SCHEMA_VERSION = 1

# action_type 枚举（report_review_actions 表的 CHECK 约束同步维护）。
ACTION_TYPE_FINALIZE = "finalize"
ACTION_TYPE_REWRITE = "rewrite"
ACTION_TYPE_RESEARCH = "research"
ACTION_TYPE_HUMAN_REVIEW = "human_review"
ACTION_TYPES = (
    ACTION_TYPE_FINALIZE,
    ACTION_TYPE_REWRITE,
    ACTION_TYPE_RESEARCH,
    ACTION_TYPE_HUMAN_REVIEW,
)

# human decision 枚举（human_review_decisions 表的 CHECK 约束同步维护）。
HUMAN_DECISION_APPROVE = "approve"
HUMAN_DECISION_REWRITE = "rewrite"
HUMAN_DECISION_RESEARCH = "research"
HUMAN_DECISION_CANCEL = "cancel"
HUMAN_DECISIONS = (
    HUMAN_DECISION_APPROVE,
    HUMAN_DECISION_REWRITE,
    HUMAN_DECISION_RESEARCH,
    HUMAN_DECISION_CANCEL,
)

# 人工 comment 上限（spec K：trim、最大 1000 字符）。
MAX_COMMENT_LENGTH = 1000

# research_need_codes（research action_payload 专用，spec I）由 issue_type
# canonical 映射；Research Planner 后续基于这些结构生成任务，**本阶段不自动生成
# 搜索 query / 不调 Chroma / 不访问 web**。
RESEARCH_NEED_CODE_MISSING_SUPPORT = "missing_support"
RESEARCH_NEED_CODE_STRONGER_SOURCE = "stronger_source"
RESEARCH_NEED_CODE_FRESHER_EVIDENCE = "fresher_evidence"
RESEARCH_NEED_CODE_ADDITIONAL_EVIDENCE = "additional_evidence"

RESEARCH_NEED_CODE_BY_ISSUE_TYPE = {
    "unsupported_by_evidence": RESEARCH_NEED_CODE_MISSING_SUPPORT,
    "weak_source_quality": RESEARCH_NEED_CODE_STRONGER_SOURCE,
    "stale_or_temporally_misaligned": RESEARCH_NEED_CODE_FRESHER_EVIDENCE,
    "insufficient_evidence": RESEARCH_NEED_CODE_ADDITIONAL_EVIDENCE,
}


# ------------------------------------------------------------------ result 投影


@dataclass(frozen=True)
class ReviewActionResult:
    """一次 ReviewActionPlan 的摘要（不含 issue 明细 / prompt / raw response）。"""

    review_action_id: UUID
    audit_id: UUID
    report_id: UUID
    action_schema_version: int
    action_type: str
    action_payload: dict
    action_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class HumanReviewRequestResult:
    """一次 human review request 的摘要。"""

    human_request_id: UUID
    review_action_id: UUID
    request_schema_version: int
    request_payload: dict
    request_fingerprint: str
    replayed: bool


@dataclass(frozen=True)
class HumanReviewDecisionResult:
    """一次人工裁决的摘要（人工决定是新的 immutable artifact，不修改 Audit）。"""

    human_decision_id: UUID
    human_request_id: UUID
    decision_schema_version: int
    decision: str
    comment: str | None
    decided_at: datetime
    decision_fingerprint: str
    replayed: bool


# ------------------------------------------------------------------ verified 产物


@dataclass(frozen=True)
class VerifiedReviewAction:
    """`verify_review_action_integrity` 的 read-side 产物（完整重建验证通过）。

    - verified_audit：上游已验证 Audit（Stage 5E.1 只消费 `VerifiedReportAudit`，
      spec A）；
    - action_type / action_payload：从 verified_audit 重新派生并逐一对比。
    """

    review_action_id: UUID
    audit_id: UUID
    report_id: UUID
    action_schema_version: int
    action_type: str
    action_payload: dict
    action_fingerprint: str
    created_at: datetime
    verified_audit: VerifiedReportAudit


@dataclass(frozen=True)
class VerifiedHumanReviewRequest:
    """`verify_human_request_integrity` 的 read-side 产物。"""

    human_request_id: UUID
    review_action_id: UUID
    request_schema_version: int
    request_payload: dict
    request_fingerprint: str
    created_at: datetime
    verified_action: VerifiedReviewAction


@dataclass(frozen=True)
class VerifiedHumanReviewDecision:
    """`verify_human_decision_integrity` 的 read-side 产物（核对 immutable fields）。"""

    human_decision_id: UUID
    human_request_id: UUID
    decision_schema_version: int
    decision: str
    comment: str | None
    decided_at: datetime
    decision_fingerprint: str
    created_at: datetime
    verified_request: VerifiedHumanReviewRequest


# ------------------------------------------------------------------ 指纹


def _canonical_dumps(payload: dict) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def compute_action_fingerprint(
    *,
    action_schema_version: int,
    audit_id: UUID,
    audit_fingerprint: str,
    report_id: UUID,
    report_fingerprint: str,
    action_type: str,
    action_payload: dict,
) -> str:
    """ReviewAction 指纹（spec M）：schema + audit/report 指纹 + action_type +
    normalized payload。**不得包含** review_action_id / created_at。"""
    payload = {
        "action_schema_version": action_schema_version,
        "audit_id": str(audit_id),
        "audit_fingerprint": audit_fingerprint,
        "report_id": str(report_id),
        "report_fingerprint": report_fingerprint,
        "action_type": action_type,
        "action_payload": action_payload,
    }
    return hashlib.sha256(_canonical_dumps(payload)).hexdigest()


def compute_request_fingerprint(
    *,
    request_schema_version: int,
    review_action_id: UUID,
    action_fingerprint: str,
    request_payload: dict,
) -> str:
    """HumanRequest 指纹（spec M）：schema + review_action_id + action_fingerprint +
    normalized payload。**不得包含** human_request_id / created_at。"""
    payload = {
        "request_schema_version": request_schema_version,
        "review_action_id": str(review_action_id),
        "action_fingerprint": action_fingerprint,
        "request_payload": request_payload,
    }
    return hashlib.sha256(_canonical_dumps(payload)).hexdigest()


def compute_decision_fingerprint(
    *,
    decision_schema_version: int,
    human_request_id: UUID,
    request_fingerprint: str,
    decision: str,
    comment: str | None,
) -> str:
    """HumanDecision 指纹（spec M）：schema + human_request_id + request_fingerprint +
    decision + normalized comment。**不得包含** human_decision_id / decided_at /
    created_at。"""
    payload = {
        "decision_schema_version": decision_schema_version,
        "human_request_id": str(human_request_id),
        "request_fingerprint": request_fingerprint,
        "decision": decision,
        "comment": comment,
    }
    return hashlib.sha256(_canonical_dumps(payload)).hexdigest()
