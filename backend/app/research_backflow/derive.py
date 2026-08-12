"""Deterministic research backflow payload derivation (stage 5E.2B + 7A.2B.3).

本模块只有纯函数，**0 LLM / 0 检索 / 0 Chroma**：

1. `derive_research_request_payload`（spec H）——从 `VerifiedReviewAction`
   （± `VerifiedHumanReviewDecision`）派生结构化交接 payload：
   - direct research（action_type=research，无 human decision）：复用 action_payload
     的 review_issue_ids / target_section_ids / related_claim_ids /
     related_evidence_card_ids / research_need_codes（derive_action_payload 已处理
     route-priority 提升：mixed issues → 全部保留）；
   - human research（action_type=human_review + decision=research）：保留 underlying
     Audit **全部** ReviewIssues，research_need_codes 恒含
     `human_requested_research` + issue_type 映射 codes（missing_support /
     stronger_source / fresher_evidence / additional_evidence）。
   不自动生成搜索 query。
2. `derive_research_backflow_plan_payload`（spec K）——从 `VerifiedResearchBackflowRequest`
   派生**确定性补充研究计划** payload（`need_specs[]`）：
   - 按 issue_type（`SUPPLEMENTAL_RESEARCH_NEED_CODES` 白名单）分组 issue；
   - 每个 need_spec：target_section_ids / related_claim_ids /
     related_evidence_card_ids（union，canonical 排序）+ retrieval_queries（冻结
     确定性模板：section context + research question / Claim statement + need
     描述；max query = `MAX_QUERIES_PER_NEED`）+ allowed_source_types（按 need
     code 的真实 source 词表）；
   - **不保存** model reasoning / prompt / secret；query 不是 LLM 生成。
"""

from uuid import UUID

from app.domain.source_records import SourceDocumentType
from app.research_backflow.contracts import (
    MAX_QUERIES_PER_NEED,
    RESEARCH_BACKFLOW_MANUAL_REASON_STRUCTURED_DATA_REFRESH,
    RESEARCH_NEED_CODE_HUMAN_REQUESTED_RESEARCH,
    RESEARCH_NEED_DESCRIPTIONS,
    SUPPLEMENTAL_RESEARCH_NEED_CODES,
    VerifiedResearchBackflowRequest,
)
from app.research_backflow.errors import ResearchBackflowIllegalTrigger
from app.review.contracts import (
    ACTION_TYPE_HUMAN_REVIEW,
    ACTION_TYPE_RESEARCH,
    HUMAN_DECISION_RESEARCH,
    RESEARCH_NEED_CODE_BY_ISSUE_TYPE,
    VerifiedHumanReviewDecision,
    VerifiedReviewAction,
)

# weak_source_quality 只允许官方披露类来源（news 来源质量不可控 → 排除）。
_ALL_OFFICIAL_DOCUMENT_TYPES = (
    SourceDocumentType.ANNUAL_REPORT.value,
    SourceDocumentType.SEMIANNUAL_REPORT.value,
    SourceDocumentType.QUARTERLY_REPORT.value,
    SourceDocumentType.COMPANY_ANNOUNCEMENT.value,
    SourceDocumentType.ISSUER_IR_MATERIAL.value,
    SourceDocumentType.PROSPECTUS.value,
)
# 其余 need 允许全部文档类来源（macro_dataset 由 Macro service 另行处理）。
_ALL_DOCUMENT_TYPES = _ALL_OFFICIAL_DOCUMENT_TYPES + (SourceDocumentType.NEWS_ARTICLE.value,)


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


def derive_research_backflow_plan_payload(
    verified_request: VerifiedResearchBackflowRequest,
    claim_statements: dict[UUID, str],
) -> dict:
    """从 verified research request 派生**确定性补充研究计划** payload（spec K）。

    - 按 `issue_type`（`SUPPLEMENTAL_RESEARCH_NEED_CODES` 白名单）分组 audit
      issues；每个 code → 一个 need_spec；
    - `related_claim_ids` / `related_evidence_card_ids` / `target_section_ids`
      是该组 issues 的 union（canonical 排序）；
    - `retrieval_queries`：冻结确定性模板（section context + research question /
      Claim statement + need 描述），**上限 = `MAX_QUERIES_PER_NEED`**；
    - `allowed_source_types`：真实 source 词表按 need code 过滤（weak_source_quality
      只允许官方披露类）；
    - `manual_required_reason`：v1 计划阶段恒不写（执行阶段在「无满足 source」时
      由执行器决定 manual_required，不假装完成）。

    **不保存** model reasoning / prompt / secret；0 LLM / 0 检索。
    """
    research_question = verified_request.verified_source_synthesis.research_question
    outline = verified_request.verified_report.verified_outline
    section_titles = {section.section_id: section.title for section in outline.sections}

    groups: dict[str, dict[str, set]] = {}
    for issue in verified_request.verified_action.verified_audit.issues:
        code = issue.issue_type
        if code not in SUPPLEMENTAL_RESEARCH_NEED_CODES:
            continue
        group = groups.setdefault(code, {"sections": set(), "claims": set(), "evidence": set()})
        group["sections"].add(issue.section_id)
        group["claims"].update(issue.related_claim_ids)
        group["evidence"].update(issue.related_evidence_card_ids)

    need_specs: list[dict] = []
    for code in sorted(groups):
        group = groups[code]
        section_ids = sorted(group["sections"])
        related_claim_ids = sorted(group["claims"])
        related_evidence_card_ids = sorted(group["evidence"])
        claims_by_id = {
            claim_id: statement
            for claim_id, statement in claim_statements.items()
            if str(claim_id) in related_claim_ids
        }
        section_context = section_titles.get(section_ids[0]) if section_ids else None
        need_specs.append(
            {
                "need_code": code,
                "target_section_ids": section_ids,
                "related_claim_ids": related_claim_ids,
                "related_evidence_card_ids": related_evidence_card_ids,
                "retrieval_queries": _build_retrieval_queries(
                    research_question=research_question,
                    claim_statements=claims_by_id,
                    need_code=code,
                    section_context=section_context,
                ),
                "allowed_source_types": _allowed_source_types(code),
            }
        )

    # 7A.2B.3 scope 冻结：非白名单（structured）issue——financial/macro/valuation
    # refresh / 证据一致性核对等——不在 automatic 文档补充研究范围（需 provider /
    # network 或新数据）。plan 只派生文档类 need_specs；structured 需求投影为
    # `manual_required_reasons` 信号，供 verify_progress 给稳定 manual reason
    # （structured_data_refresh_required），**不误报 research_backflow_no_progress**。
    structured_issue_types = sorted(
        {
            issue.issue_type
            for issue in verified_request.verified_action.verified_audit.issues
            if issue.issue_type not in SUPPLEMENTAL_RESEARCH_NEED_CODES
        }
    )
    return {
        "need_specs": need_specs,
        "max_queries_per_need": MAX_QUERIES_PER_NEED,
        "manual_required_reasons": (
            [RESEARCH_BACKFLOW_MANUAL_REASON_STRUCTURED_DATA_REFRESH]
            if structured_issue_types
            else []
        ),
    }


def _build_retrieval_queries(
    *,
    research_question: str,
    claim_statements: dict[UUID, str],
    need_code: str,
    section_context: str | None,
) -> list[str]:
    """冻结确定性 query 模板（spec K：research question / Claim statement +
    need 描述 + section context；**禁止 LLM 自由生成 web query**）。

    第一条始终是 base query（research question + need 描述）；随后每个 related
    Claim statement 一条，**总条数上限 `MAX_QUERIES_PER_NEED`**。claim 按 claim_id
    canonical 排序保证确定性。
    """
    need_desc = RESEARCH_NEED_DESCRIPTIONS.get(need_code, need_code)
    context = f"{section_context}：" if section_context else ""
    queries = [f"{context}{research_question}（{need_desc}）"]
    for claim_id in sorted(claim_statements, key=str):
        if len(queries) >= MAX_QUERIES_PER_NEED:
            break
        queries.append(f"{context}{claim_statements[claim_id]}（{need_desc}）")
    return queries


def _allowed_source_types(need_code: str) -> list[str]:
    """按 need code 返回真实 source 词表（canonical 排序）。

    `weak_source_quality` 只允许官方披露类（排除 news_article）；其余 need 允许
    全部文档类来源（macro_dataset 由 Macro service 另行处理，不在文档检索路径）。
    """
    if need_code == "weak_source_quality":
        return sorted(_ALL_OFFICIAL_DOCUMENT_TYPES)
    return sorted(_ALL_DOCUMENT_TYPES)
