"""Deterministic research request payload derivation (stage 5E.2B, spec H).

从 `VerifiedReviewAction`（± `VerifiedHumanReviewDecision`）纯函数派生结构化交接
payload。**0 LLM / 0 检索 / 0 Chroma / 0 query 生成**——只建立稳定、可回放、可审计
的交接 payload。

- direct research（action_type=research，无 human decision）：复用 action_payload
  的 review_issue_ids / target_section_ids / related_claim_ids /
  related_evidence_card_ids / research_need_codes（derive_action_payload 已处理
  route-priority 提升：mixed issues → 全部保留）；
- human research（action_type=human_review + decision=research）：保留 underlying
  Audit **全部** ReviewIssues（spec H），research_need_codes 恒含
  `human_requested_research` + issue_type 映射 codes（missing_support /
  stronger_source / fresher_evidence / additional_evidence）。

request_payload 只含结构化 IDs / codes，**不写长 prose / 不自动生成搜索 query**。
"""

from app.research_backflow.contracts import RESEARCH_NEED_CODE_HUMAN_REQUESTED_RESEARCH
from app.research_backflow.errors import ResearchBackflowIllegalTrigger
from app.review.contracts import (
    ACTION_TYPE_HUMAN_REVIEW,
    ACTION_TYPE_RESEARCH,
    HUMAN_DECISION_RESEARCH,
    RESEARCH_NEED_CODE_BY_ISSUE_TYPE,
    VerifiedHumanReviewDecision,
    VerifiedReviewAction,
)


def derive_research_request_payload(
    verified_action: VerifiedReviewAction,
    verified_decision: VerifiedHumanReviewDecision | None,
) -> dict:
    """从 verified action（± decision）派生交接 payload（caller 不提供字段）。

    legal trigger（spec G）由 service 层先校验；此处再防御性 double-check，任何
    不一致 → `ResearchBackflowIllegalTrigger`（0 write）。
    """
    if verified_decision is None:
        # (A) direct research：复用 action 的 research 派生字段。
        if verified_action.action_type != ACTION_TYPE_RESEARCH:
            raise ResearchBackflowIllegalTrigger(
                "research request without a human decision requires a research action"
            )
        action_payload = verified_action.action_payload
        return {
            "review_issue_ids": sorted(action_payload["review_issue_ids"]),
            "target_section_ids": sorted(action_payload["target_section_ids"]),
            "related_claim_ids": sorted(action_payload.get("related_claim_ids", [])),
            "related_evidence_card_ids": sorted(
                action_payload.get("related_evidence_card_ids", [])
            ),
            "research_need_codes": sorted(action_payload["research_need_codes"]),
        }

    # (B) human research：保留 underlying Audit 全部 relevant ReviewIssues。
    if verified_action.action_type != ACTION_TYPE_HUMAN_REVIEW:
        raise ResearchBackflowIllegalTrigger("human research requires a human_review action")
    if verified_decision.decision != HUMAN_DECISION_RESEARCH:
        raise ResearchBackflowIllegalTrigger("human research requires a research human decision")
    issues = list(verified_action.verified_audit.issues)
    need_codes = {RESEARCH_NEED_CODE_HUMAN_REQUESTED_RESEARCH}
    for issue in issues:
        code = RESEARCH_NEED_CODE_BY_ISSUE_TYPE.get(issue.issue_type)
        if code is not None:
            need_codes.add(code)
    return {
        "review_issue_ids": sorted(str(issue.review_issue_id) for issue in issues),
        "target_section_ids": sorted({issue.section_id for issue in issues}),
        "related_claim_ids": sorted(
            {claim_id for issue in issues for claim_id in issue.related_claim_ids}
        ),
        "related_evidence_card_ids": sorted(
            {evidence_id for issue in issues for evidence_id in issue.related_evidence_card_ids}
        ),
        "research_need_codes": sorted(need_codes),
    }
