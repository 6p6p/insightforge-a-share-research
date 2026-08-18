"""Deterministic report checks tests (stage 5C, spec Q/R/T): 纯函数（无 DB / 0 LLM）。

覆盖（spec T checks 清单）：
- all pass happy path（theme + risks_and_gaps，conflict/gap 显式引用）；
- 缺失 conflict 保留 / 缺失 gap 保留 → conflict_gap_preservation finding；
- unbound Evidence → evidence_reference_closure finding；
- numeric 未 grounding → numeric_grounding finding；
- 禁用投资语言 → forbidden_investment_language finding；
- 内联 alias 泄漏 → internal_alias_leak finding；
- Claim 越出 section allowed set → claim_reference_closure finding；
- Evidence → source provenance 缺失 → citation_provenance_closure finding；
- 空 section / outline coverage / draft_section_integrity → 对应 finding；
- deterministic findings order（code + section_order + paragraph_index）。
"""

import copy

from app.report.checks import EvidenceCheckData, run_checks
from app.report.contracts import CheckFinding
from tests.report.helpers import EVIDENCE_IDS, make_scenario


def _codes(findings: list[CheckFinding]) -> list[str]:
    return [finding.code for finding in findings]


def test_checks_all_pass_happy_path() -> None:
    scenario = make_scenario()
    findings = run_checks(scenario.check_input())
    assert findings == []


# ---------------------------------------------------------------- coverage / integrity


def test_check_outline_section_coverage_missing_section() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    payload["sections"] = [s for s in payload["sections"] if s["section_id"] != "S2"]
    findings = run_checks(scenario.check_input(report_payload=payload))
    assert "outline_section_coverage" in _codes(findings)


def test_check_outline_section_coverage_extra_section() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    payload["sections"].append(
        {
            "section_id": "S9",
            "section_order": 9,
            "section_type": "theme",
            "title": "额外",
            "paragraphs": [],
        }
    )
    findings = run_checks(scenario.check_input(report_payload=payload))
    assert "outline_section_coverage" in _codes(findings)


def test_check_draft_section_integrity_mismatch() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S2":
            section["draft_section_id"] = "00000000-0000-0000-0000-000000000000"
    findings = run_checks(scenario.check_input(report_payload=payload))
    assert "draft_section_integrity" in _codes(findings)


# ---------------------------------------------------------------- claim / evidence closure


def test_check_claim_reference_closure_out_of_scope() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S2":
            # S2 allowed = {C3, C4}；引用 C1 → 越出 section。
            section["paragraphs"][0]["claim_ids"].append(
                str(scenario.outline.verified_synthesis_result.alias_map["C1"])
            )
    findings = run_checks(scenario.check_input(report_payload=payload))
    closure = [f for f in findings if f.code == "claim_reference_closure"]
    assert closure
    assert closure[0].section_id == "S2"


def test_check_evidence_reference_closure_unbound() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S1":
            # S1 references C1/C2；E2 只绑定 C3 → unbound。
            section["paragraphs"][0]["evidence_card_ids"].append(str(EVIDENCE_IDS["E2"]))
    findings = run_checks(scenario.check_input(report_payload=payload))
    closure = [f for f in findings if f.code == "evidence_reference_closure"]
    assert closure
    assert closure[0].section_id == "S1"
    assert closure[0].related_evidence_card_ids == (str(EVIDENCE_IDS["E2"]),)


def test_check_citation_provenance_closure_missing_provenance() -> None:
    scenario = make_scenario()
    broken = dict(scenario.evidence)
    broken[str(EVIDENCE_IDS["E2"])] = EvidenceCheckData(
        evidence_card_id=EVIDENCE_IDS["E2"],
        evidence_statement="公司产能利用率达85%。",
        quote_text="产能利用率85%",
        origin_type="document_chunk",
        has_provenance=False,  # 模拟 source_id 被删
        bound_claim_ids=(scenario.outline.verified_synthesis_result.alias_map["C3"],),
    )
    findings = run_checks(scenario.check_input(evidence=broken))
    closure = [f for f in findings if f.code == "citation_provenance_closure"]
    assert closure
    assert str(EVIDENCE_IDS["E2"]) in closure[0].related_evidence_card_ids


# ---------------------------------------------------------------- paragraph policy / grounding


def test_check_numeric_grounding_ungrounded() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S2":
            section["paragraphs"][0]["text"] = "公司产能利用率达99%。"
    findings = run_checks(scenario.check_input(report_payload=payload))
    numeric = [f for f in findings if f.code == "numeric_grounding"]
    assert numeric
    assert numeric[0].section_id == "S2"
    assert numeric[0].paragraph_index == 0


def test_check_forbidden_investment_language() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S2":
            section["paragraphs"][0]["text"] = "建议买入该股票。"
    findings = run_checks(scenario.check_input(report_payload=payload))
    assert "forbidden_investment_language" in _codes(findings)


def test_check_internal_alias_leak() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S2":
            section["paragraphs"][0]["text"] = "详见（C3）相关分析。"
    findings = run_checks(scenario.check_input(report_payload=payload))
    leak = [f for f in findings if f.code == "internal_alias_leak"]
    assert leak
    assert leak[0].section_id == "S2"


# ---------------------------------------------------------------- conflict / gap preservation


def test_check_conflict_gap_preservation_missing_conflict() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S3":
            section["paragraphs"][0]["conflict_indexes"] = []
    findings = run_checks(scenario.check_input(report_payload=payload))
    preservation = [f for f in findings if f.code == "conflict_gap_preservation"]
    assert preservation
    assert preservation[0].section_id == "S3"


def test_check_conflict_gap_preservation_missing_gap() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S3":
            section["paragraphs"][0]["evidence_gap_indexes"] = []
    findings = run_checks(scenario.check_input(report_payload=payload))
    preservation = [f for f in findings if f.code == "conflict_gap_preservation"]
    assert preservation
    assert preservation[0].section_id == "S3"


def test_check_conflict_gap_preservation_zero_links_still_fails() -> None:
    """P0.5：S3（risks_and_gaps）0 claim + 0 evidence 且 outline 声明了
    conflict/gap indexes 时，报告未引用 → 必须仍 FAIL。
    structural finding 本就不带 claim/evidence 链接（0 links 是预期，
    不是 audit linkage bug）。"""
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S3":
            for paragraph in section["paragraphs"]:
                paragraph["conflict_indexes"] = []
                paragraph["evidence_gap_indexes"] = []
    findings = run_checks(scenario.check_input(report_payload=payload))
    preservation = [f for f in findings if f.code == "conflict_gap_preservation"]
    assert preservation
    assert preservation[0].section_id == "S3"
    assert preservation[0].related_claim_ids == ()
    assert preservation[0].related_evidence_card_ids == ()


def test_check_conflict_gap_preservation_empty_outline_ok() -> None:
    """P0.5：outline 未声明任何 conflict/gap index → 报告 0 引用 → 不 FAIL。"""
    from dataclasses import replace

    scenario = make_scenario()
    outline = scenario.outline
    rebuilt = []
    for s in outline.sections:
        if s.section_id == "S3":
            rebuilt.append(replace(s, conflict_indexes=(), evidence_gap_indexes=()))
        else:
            rebuilt.append(s)
    outline = replace(outline, sections=tuple(rebuilt))
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S3":
            for paragraph in section["paragraphs"]:
                paragraph["conflict_indexes"] = []
                paragraph["evidence_gap_indexes"] = []
    findings = run_checks(scenario.check_input(report_payload=payload, verified_outline=outline))
    preservation = [f for f in findings if f.code == "conflict_gap_preservation"]
    assert not preservation


# ---------------------------------------------------------------- empty section


def test_check_empty_section() -> None:
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S2":
            section["paragraphs"] = []
    findings = run_checks(scenario.check_input(report_payload=payload))
    empty = [f for f in findings if f.code == "empty_section"]
    assert empty
    assert empty[0].section_id == "S2"


# ---------------------------------------------------------------- deterministic order


def test_checks_deterministic_findings_order() -> None:
    """findings 按 (code, section_order, paragraph_index) 确定性排序（spec T）。"""
    scenario = make_scenario()
    payload = copy.deepcopy(scenario.report_payload())
    for section in payload["sections"]:
        if section["section_id"] == "S1":
            section["paragraphs"][0]["text"] = (
                "详见（C1）相关分析。"  # internal_alias_leak (order 1)
            )
        if section["section_id"] == "S2":
            section["paragraphs"][0]["text"] = "建议卖出该股票。"  # forbidden (order 2)
        if section["section_id"] == "S3":
            section["paragraphs"][0]["text"] = "目标价太高。"  # forbidden (order 3)
    findings = run_checks(scenario.check_input(report_payload=payload))
    order = [(f.code, f.section_id) for f in findings]
    expected = [
        ("forbidden_investment_language", "S2"),
        ("forbidden_investment_language", "S3"),
        ("internal_alias_leak", "S1"),
    ]
    # forbidden code < internal code；同 code 按 section_order。
    assert order == expected
