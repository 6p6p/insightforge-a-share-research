"""补充研究计划派生纯函数单测（7A.2B.3 spec K）：**0 DB / 0 LLM / 0 检索**。

覆盖：
- `derive_research_backflow_plan_payload`：按 issue_type（白名单）分组 →
  need_specs（union + canonical 排序）；非白名单 issue 不进计划；冻结 query
  模板（base query + Claim statement，上限 `MAX_QUERIES_PER_NEED`）；
  `allowed_source_types` 按 need code（weak_source_quality 只允许官方披露类）；
- `compute_research_backflow_plan_fingerprint`：确定性 / canonical SHA-256。
"""

import hashlib
import json
from datetime import date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

from app.analysis.synthesis.contracts import VerifiedSynthesisResult
from app.audit.contracts import ReviewIssue, VerifiedReportAudit
from app.domain.source_records import SourceDocumentType
from app.report.contracts import VerifiedReport
from app.report_outline.contracts import OutlineSection, VerifiedReportOutline
from app.research_backflow.contracts import (
    MAX_QUERIES_PER_NEED,
    RESEARCH_NEED_DESCRIPTIONS,
    SUPPLEMENTAL_RESEARCH_STRATEGY_NAME,
    SUPPLEMENTAL_RESEARCH_STRATEGY_VERSION,
    VerifiedResearchBackflowRequest,
    compute_research_backflow_plan_fingerprint,
)
from app.research_backflow.derive import derive_research_backflow_plan_payload
from app.review.contracts import VerifiedReviewAction

_QUESTION = "某公司2025年扣非净利润增长是否有足够证据支撑？"


def _issue(
    *,
    issue_type: str,
    section_id: str = "S1",
    claim_ids=(),
    evidence_ids=(),
) -> ReviewIssue:
    return ReviewIssue(
        review_issue_id=uuid4(),
        audit_id=uuid4(),
        ordinal=1,
        issue_type=issue_type,
        severity="normal",
        section_id=section_id,
        paragraph_index=0,
        message="测试 issue",
        related_claim_ids=tuple(claim_ids),
        related_evidence_card_ids=tuple(evidence_ids),
    )


def _section(section_id: str, title: str, claim_ids=()) -> OutlineSection:
    return OutlineSection(
        section_id=section_id,
        section_order=int(section_id[1:]),
        section_type="analysis",
        title=title,
        claim_ids=tuple(claim_ids),
        conflict_indexes=(),
        evidence_gap_indexes=(),
    )


def _verified_request(
    *,
    issues: list[ReviewIssue],
    sections: tuple[OutlineSection, ...] = (
        _section("S1", "营业收入分析"),
        _section("S2", "毛利率分析"),
    ),
    research_question: str = _QUESTION,
) -> VerifiedResearchBackflowRequest:
    """最小 VerifiedResearchBackflowRequest（derive 只读 research_question /
    outline.sections / audit.issues；其余字段占位）。"""
    company_id = uuid4()
    rq_sha = hashlib.sha256(research_question.encode("utf-8")).hexdigest()
    as_of = date(2025, 12, 31)

    synthesis = object.__new__(VerifiedSynthesisResult)
    for _field, _value in {
        "synthesis_result_id": uuid4(),
        "synthesis_id": uuid4(),
        "company_id": company_id,
        "research_question": research_question,
        "research_question_sha256": rq_sha,
        "analysis_as_of": as_of,
        "synthesis_fingerprint": "0" * 64,
        "result_fingerprint": "0" * 64,
        "input_claim_ids": (),
        "alias_map": {},
        "output": None,
    }.items():
        object.__setattr__(synthesis, _field, _value)
    outline = VerifiedReportOutline(
        outline_id=uuid4(),
        synthesis_result_id=synthesis.synthesis_id,
        company_id=company_id,
        research_question_sha256=rq_sha,
        analysis_as_of=as_of,
        outline_schema_version=1,
        outline_fingerprint="0" * 64,
        sections=sections,
        verified_synthesis_result=synthesis,
    )
    report = VerifiedReport(
        report_id=uuid4(),
        outline_id=outline.outline_id,
        company_id=company_id,
        research_question_sha256=rq_sha,
        analysis_as_of=as_of,
        report_schema_version=1,
        report_fingerprint="0" * 64,
        report_payload={},
        verified_outline=outline,
        verified_drafts=(),
    )
    audit = VerifiedReportAudit(
        audit_id=uuid4(),
        report_id=report.report_id,
        check_result_id=uuid4(),
        audit_schema_version=1,
        auditor_name="evidence_bound_report_auditor",
        auditor_version=1,
        auditor_model_id="deepseek:deepseek-v4-flash",
        audit_input_fingerprint="0" * 64,
        audit_status="fail",
        recommended_route="research",
        issue_count=len(issues),
        audit_fingerprint="0" * 64,
        issues=tuple(issues),
        verified_report=report,
        verified_check=SimpleNamespace(status="pass"),
    )
    action = VerifiedReviewAction(
        review_action_id=uuid4(),
        audit_id=audit.audit_id,
        report_id=report.report_id,
        action_schema_version=1,
        action_type="research",
        action_payload={},
        action_fingerprint="0" * 64,
        created_at=datetime(2026, 8, 12, 9, 0, 0),
        verified_audit=audit,
    )
    return VerifiedResearchBackflowRequest(
        research_request_id=uuid4(),
        source_stage5_run_id=uuid4(),
        review_action_id=action.review_action_id,
        human_decision_id=None,
        source_report_id=report.report_id,
        company_id=company_id,
        research_question_sha256=rq_sha,
        analysis_as_of=as_of,
        request_schema_version=1,
        request_payload={},
        request_fingerprint="1" * 64,
        created_at=datetime(2026, 8, 12, 9, 0, 0),
        verified_action=action,
        verified_decision=None,
        verified_report=report,
        verified_source_synthesis=synthesis,
    )


def _claims(num: int) -> dict[UUID, str]:
    return {
        UUID(f"00000000-0000-4000-8000-{i:012d}"): f"claim statement {i}" for i in range(1, num + 1)
    }


def _need_codes(payload: dict) -> list[str]:
    return [spec["need_code"] for spec in payload["need_specs"]]


# ---------------------------------------------------------------- 分组（spec K whitelist）


def test_plan_groups_by_whitelist_need_code_union_canonical() -> None:
    """白名单 issue_type → need_spec；union + canonical 排序；非白名单跳过。"""
    request = _verified_request(
        issues=[
            _issue(
                issue_type="unsupported_by_evidence",
                section_id="S2",
                claim_ids=("00000000-0000-4000-8000-000000000003",),
                evidence_ids=("b3", "a1"),
            ),
            _issue(
                issue_type="unsupported_by_evidence",
                section_id="S1",
                claim_ids=("00000000-0000-4000-8000-000000000001",),
                evidence_ids=("a1",),
            ),
            _issue(issue_type="wording_overclaim"),  # 非白名单 → 不进计划
        ]
    )
    payload = derive_research_backflow_plan_payload(request, _claims(3))
    assert _need_codes(payload) == ["unsupported_by_evidence"]
    assert payload["max_queries_per_need"] == MAX_QUERIES_PER_NEED

    spec = payload["need_specs"][0]
    assert spec["target_section_ids"] == ["S1", "S2"]
    assert spec["related_claim_ids"] == [
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000003",
    ]
    # evidence ids 是字符串，canonical 排序。
    assert spec["related_evidence_card_ids"] == ["a1", "b3"]


def test_plan_multiple_whitelist_codes_each_a_need_spec() -> None:
    """多个白名单 code → 每个一个 need_spec（canonical code 排序）。"""
    request = _verified_request(
        issues=[
            _issue(issue_type="weak_source_quality", section_id="S1"),
            _issue(issue_type="insufficient_evidence", section_id="S2"),
        ]
    )
    payload = derive_research_backflow_plan_payload(request, {})
    assert _need_codes(payload) == ["insufficient_evidence", "weak_source_quality"]


def test_plan_no_whitelist_issues_yields_empty_need_specs() -> None:
    """全部非白名单 issue → 空 need_specs（执行阶段据此 manual_required）。"""
    request = _verified_request(issues=[_issue(issue_type="wording_overclaim")])
    payload = derive_research_backflow_plan_payload(request, {})
    assert payload["need_specs"] == []
    assert payload["max_queries_per_need"] == MAX_QUERIES_PER_NEED


# ---------------------------------------------------------------- query 模板（spec K frozen）


def test_plan_query_template_frozen_and_capped() -> None:
    """query：base（section context + research question + need 描述）→ Claim 语句；
    上限 `MAX_QUERIES_PER_NEED`；Claim 按 id canonical 排序。"""
    need_code = "unsupported_by_evidence"
    claim_ids = [
        "00000000-0000-4000-8000-000000000002",
        "00000000-0000-4000-8000-000000000001",
        "00000000-0000-4000-8000-000000000003",
    ]
    request = _verified_request(
        issues=[_issue(issue_type=need_code, section_id="S1", claim_ids=claim_ids)]
    )
    payload = derive_research_backflow_plan_payload(request, _claims(3))
    spec = payload["need_specs"][0]

    desc = RESEARCH_NEED_DESCRIPTIONS[need_code]
    # section context = S1 title。
    assert spec["retrieval_queries"][0] == f"营业收入分析：{_QUESTION}（{desc}）"
    # Claim query：canonical id 序（0000001 → 0000002 → 0000003）。
    assert spec["retrieval_queries"][1] == f"营业收入分析：claim statement 1（{desc}）"
    assert spec["retrieval_queries"][2] == f"营业收入分析：claim statement 2（{desc}）"
    assert len(spec["retrieval_queries"]) == MAX_QUERIES_PER_NEED


def test_plan_query_without_section_context() -> None:
    """issue 无 section_id → query 无 section context 前缀。"""
    request = _verified_request(issues=[_issue(issue_type="causal_overreach", section_id="S9")])
    payload = derive_research_backflow_plan_payload(request, {})
    desc = RESEARCH_NEED_DESCRIPTIONS["causal_overreach"]
    assert payload["need_specs"][0]["retrieval_queries"][0] == f"{_QUESTION}（{desc}）"


def test_plan_query_unreferenced_claims_excluded() -> None:
    """claim_statements 里未被 issue 引用的 claim 不进 query（claims_by_id 过滤）。"""
    request = _verified_request(
        issues=[
            _issue(
                issue_type="weak_source_quality",
                claim_ids=("00000000-0000-4000-8000-000000000001",),
            )
        ]
    )
    payload = derive_research_backflow_plan_payload(request, _claims(3))
    queries = payload["need_specs"][0]["retrieval_queries"]
    # base + 仅 claim 1（claims 2/3 未引用 → 排除）。
    assert len(queries) == 2
    assert "claim statement 1" in queries[1]


# ---------------------------------------------------------------- allowed_source_types


def test_plan_weak_source_quality_official_disclosure_only() -> None:
    """weak_source_quality 只允许官方披露类（news_article 排除）。"""
    request = _verified_request(issues=[_issue(issue_type="weak_source_quality")])
    payload = derive_research_backflow_plan_payload(request, {})
    allowed = payload["need_specs"][0]["allowed_source_types"]
    assert SourceDocumentType.NEWS_ARTICLE.value not in allowed
    assert allowed == sorted(
        [
            SourceDocumentType.ANNUAL_REPORT.value,
            SourceDocumentType.SEMIANNUAL_REPORT.value,
            SourceDocumentType.QUARTERLY_REPORT.value,
            SourceDocumentType.COMPANY_ANNOUNCEMENT.value,
            SourceDocumentType.ISSUER_IR_MATERIAL.value,
            SourceDocumentType.PROSPECTUS.value,
        ]
    )


def test_plan_other_needs_allow_all_document_types() -> None:
    """非 weak_source_quality 的 need 允许全部文档类（含 news_article）。"""
    request = _verified_request(issues=[_issue(issue_type="insufficient_evidence")])
    payload = derive_research_backflow_plan_payload(request, {})
    allowed = payload["need_specs"][0]["allowed_source_types"]
    assert SourceDocumentType.NEWS_ARTICLE.value in allowed


# ---------------------------------------------------------------- fingerprint


def test_plan_fingerprint_deterministic_and_sensitive() -> None:
    """指纹确定性：同输入同指纹；request 或 payload 变化 → 新指纹。"""
    request = _verified_request(issues=[_issue(issue_type="weak_source_quality", section_id="S1")])
    payload = derive_research_backflow_plan_payload(request, {})
    kwargs = {
        "plan_schema_version": 1,
        "research_backflow_request_id": request.research_request_id,
        "request_fingerprint": request.request_fingerprint,
        "strategy_name": SUPPLEMENTAL_RESEARCH_STRATEGY_NAME,
        "strategy_version": SUPPLEMENTAL_RESEARCH_STRATEGY_VERSION,
        "plan_payload": payload,
    }
    fp1 = compute_research_backflow_plan_fingerprint(**kwargs)
    fp2 = compute_research_backflow_plan_fingerprint(**kwargs)
    assert fp1 == fp2
    assert len(fp1) == 64

    # 不同 strategy_version / 不同 request_fingerprint / 不同 payload → 新指纹。
    kwargs["strategy_version"] = 2
    assert compute_research_backflow_plan_fingerprint(**kwargs) != fp1
    kwargs["strategy_version"] = 1
    kwargs["request_fingerprint"] = "2" * 64
    assert compute_research_backflow_plan_fingerprint(**kwargs) != fp1


def test_plan_fingerprint_is_sha256_of_canonical_json() -> None:
    """指纹 = canonical JSON（sort_keys + ensure_ascii=False + 紧分隔符）SHA-256。"""
    payload = {"need_specs": [], "max_queries_per_need": MAX_QUERIES_PER_NEED}
    request = _verified_request(issues=[])

    canonical = json.dumps(
        {
            "plan_schema_version": 1,
            "research_backflow_request_id": str(request.research_request_id),
            "request_fingerprint": request.request_fingerprint,
            "strategy_name": SUPPLEMENTAL_RESEARCH_STRATEGY_NAME,
            "strategy_version": SUPPLEMENTAL_RESEARCH_STRATEGY_VERSION,
            "plan_payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    expected = hashlib.sha256(canonical).hexdigest()
    assert (
        compute_research_backflow_plan_fingerprint(
            plan_schema_version=1,
            research_backflow_request_id=request.research_request_id,
            request_fingerprint=request.request_fingerprint,
            strategy_name=SUPPLEMENTAL_RESEARCH_STRATEGY_NAME,
            strategy_version=SUPPLEMENTAL_RESEARCH_STRATEGY_VERSION,
            plan_payload=payload,
        )
        == expected
    )
