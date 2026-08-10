"""Deterministic report assembly tests (stage 5C, spec K/L/M): 纯函数（无 DB / 0 LLM）。

覆盖：
- exact coverage happy path：每 Outline section 恰好一个 DraftSection，sections 严格
  按 Outline order，payload 结构符合 spec M；
- missing section / duplicate section / extra section / wrong outline / identity
  不匹配（order/type/title）→ `ReportAssemblyError`（0 写）；
- `draft_section_fingerprint_data`：按 section_order 排序 + 字段完整（spec N）。
"""

from dataclasses import replace
from uuid import uuid4

import pytest

from app.report.assemble import (
    AssembledSectionDraft,
    assemble_report_payload,
    draft_section_fingerprint_data,
)
from app.report.errors import ReportAssemblyError
from app.report_outline.contracts import SECTION_TYPE_THEME
from tests.report.helpers import make_scenario


def _drafts(scenario, section_ids=("S1", "S2", "S3")) -> list[AssembledSectionDraft]:
    return [
        AssembledSectionDraft(
            verified=scenario.drafts[section_id],
            section_payload=scenario.section_payloads[section_id],
        )
        for section_id in section_ids
    ]


def test_assemble_exact_coverage_happy_path() -> None:
    scenario = make_scenario()
    payload = assemble_report_payload(verified_outline=scenario.outline, drafts=_drafts(scenario))

    sections = payload["sections"]
    assert len(sections) == 3
    # 严格按 Outline order（S1 → S2 → S3）。
    assert [s["section_order"] for s in sections] == [1, 2, 3]
    assert [s["section_id"] for s in sections] == ["S1", "S2", "S3"]
    for section in sections:
        assert set(section) == {
            "section_id",
            "section_order",
            "section_type",
            "title",
            "draft_section_id",
            "paragraphs",
        }
        assert section["draft_section_id"] == str(
            scenario.drafts[section["section_id"]].draft_section_id
        )
        assert (
            section["paragraphs"] == scenario.section_payloads[section["section_id"]]["paragraphs"]
        )
    assert sections[0]["section_type"] == SECTION_TYPE_THEME


def test_assemble_missing_section_rejected() -> None:
    scenario = make_scenario()
    with pytest.raises(ReportAssemblyError, match="missing draft section"):
        assemble_report_payload(
            verified_outline=scenario.outline, drafts=_drafts(scenario, ("S1", "S2"))
        )


def test_assemble_duplicate_section_rejected() -> None:
    scenario = make_scenario()
    draft = scenario.drafts["S1"]
    duplicate = AssembledSectionDraft(
        verified=replace(draft, draft_section_id=uuid4()),
        section_payload=scenario.section_payloads["S1"],
    )
    with pytest.raises(ReportAssemblyError, match="duplicate draft section"):
        assemble_report_payload(
            verified_outline=scenario.outline,
            drafts=_drafts(scenario) + [duplicate],
        )


def test_assemble_extra_section_rejected() -> None:
    scenario = make_scenario()
    extra = AssembledSectionDraft(
        verified=replace(scenario.drafts["S1"], section_id="S9"),
        section_payload=scenario.section_payloads["S1"],
    )
    with pytest.raises(ReportAssemblyError, match="extra draft section"):
        assemble_report_payload(
            verified_outline=scenario.outline,
            drafts=[extra, *_drafts(scenario, ("S1", "S2", "S3"))],
        )


def test_assemble_wrong_outline_rejected() -> None:
    scenario = make_scenario()
    wrong = AssembledSectionDraft(
        verified=replace(scenario.drafts["S1"], outline_id=uuid4()),
        section_payload=scenario.section_payloads["S1"],
    )
    with pytest.raises(ReportAssemblyError, match="outline_id"):
        assemble_report_payload(
            verified_outline=scenario.outline,
            drafts=[wrong, *_drafts(scenario, ("S2", "S3"))],
        )


def test_assemble_identity_mismatch_rejected() -> None:
    scenario = make_scenario()
    # title 与 Outline 不一致 → identity 拒绝。
    mismatched = AssembledSectionDraft(
        verified=replace(scenario.drafts["S1"], title="被篡改的标题"),
        section_payload=scenario.section_payloads["S1"],
    )
    with pytest.raises(ReportAssemblyError, match="title mismatch"):
        assemble_report_payload(
            verified_outline=scenario.outline,
            drafts=[mismatched, *_drafts(scenario, ("S2", "S3"))],
        )


def test_assemble_draft_section_fingerprint_data_ordered() -> None:
    scenario = make_scenario()
    data = draft_section_fingerprint_data(_drafts(scenario))
    assert [item["section_order"] for item in data] == [1, 2, 3]
    expected = {v.section_order: v for v in scenario.drafts.values()}
    for item in data:
        assert set(item) == {
            "section_order",
            "draft_section_id",
            "section_fingerprint",
            "writer_name",
            "writer_version",
            "writer_model_id",
        }
        verified = expected[item["section_order"]]
        assert item["draft_section_id"] == str(verified.draft_section_id)
        assert item["section_fingerprint"] == verified.section_fingerprint
        assert item["writer_name"] == verified.writer_name
        assert item["writer_version"] == verified.writer_version
        assert item["writer_model_id"] == verified.writer_model_id
