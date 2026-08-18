"""Stage5 build_report_draft section degradation tests (P0).

Tests that individual section DraftSectionModelUnavailable does not kill the
entire workflow — degraded sections are skipped, remaining sections proceed.

Test scenarios:
1. No degradation — all sections complete normally
2. Single section degraded — others continue
3. Multiple sections degraded — still continues
4. ALL sections degraded — assemble_report raises Stage5InvalidState (no fake report)
5. Assemble report skips degraded sections (only valid sections assembled)
"""

from uuid import UUID, uuid4

import pytest

from app.draft_section.errors import DraftSectionModelUnavailable
from app.stage5.nodes import make_build_report_draft_node, make_assemble_report_node
from app.report_outline.contracts import SECTION_TYPE_THEME, OutlineSection
from app.report.contracts import ReportAssemblyDraft, ReportResult


_NEXT_UUID = uuid4()


class _FakeOutlineService:
    """Returns a canned outline with configurable sections."""

    def __init__(self, sections: list[dict]):
        self._sections = sections
        self._outline_id = uuid4()
        self.verify_count = 0

    async def create_or_get_outline(self, synthesis_result_id):
        return _FakeOutline(self._outline_id)

    async def verify_outline_integrity(self, outline_id):
        self.verify_count += 1
        return _FakeVerifiedOutline(self._outline_id, self._sections)


class _FakeOutline:
    def __init__(self, outline_id):
        self.outline_id = outline_id


class _FakeVerifiedOutline:
    def __init__(self, outline_id, sections):
        self.outline_id = outline_id
        self.outline_fingerprint = "test-fp"
        self.company_id = uuid4()
        self.research_question_sha256 = "test-sha256"
        self.analysis_as_of = "2025-01-01"
        self.sections = [
            OutlineSection(
                section_id=s["section_id"],
                section_order=s["section_order"],
                section_type=s.get("section_type", SECTION_TYPE_THEME),
                title=s["title"],
                claim_ids=[],
                conflict_indexes=[],
                evidence_gap_indexes=[],
            )
            for s in sections
        ]


class _FakeDraftService:
    """Fake that raises DraftSectionModelUnavailable for configured section IDs."""

    def __init__(self, fail_ids: set[str]):
        self._fail_ids = fail_ids
        self.call_count = 0
        self.success_count = 0

    async def create_or_get_section(self, request):
        self.call_count += 1
        if str(request.section_id) in self._fail_ids:
            raise DraftSectionModelUnavailable("test: model unavailable")
        self.success_count += 1
        from dataclasses import dataclass

        @dataclass
        class FakeResult:
            draft_section_id: UUID
            replayed: bool = False
            outline_id: UUID = uuid4()
            section_fingerprint: str = "test-fp"
            writer_input_fingerprint: str = "test-input-fp"
            paragraph_count: int = 1

        return FakeResult(draft_section_id=uuid4())


class _FakeReportService:
    """Records calls for assertion."""

    def __init__(self):
        self.calls: list[ReportAssemblyDraft] = []
        self.report_id = uuid4()

    async def create_or_get_report(self, draft: ReportAssemblyDraft) -> ReportResult:
        self.calls.append(draft)
        return ReportResult(
            report_id=self.report_id,
            outline_id=draft.outline_id,
            company_id=uuid4(),
            research_question_sha256="test-sha256",
            analysis_as_of="2025-01-01",
            report_schema_version="1",
            report_fingerprint="test-report-fp",
            replayed=False,
            section_count=len(draft.draft_section_ids),
        )


def _make_deps(sections: list[dict], fail_ids: set[str] | None = None):
    """Build a plain object that quacks like Stage5WorkflowDependencies."""
    if fail_ids is None:
        fail_ids = set()
    d = type("FakeDeps", (), {})()
    d.report_outline_service = _FakeOutlineService(sections)
    d.draft_section_service = _FakeDraftService(fail_ids)
    d.report_service = _FakeReportService()
    d.sessionmaker = None
    d.report_check_service = None
    d.report_audit_service = None
    d.review_action_service = None
    d.revision_service = None
    d.research_backflow_service = None
    return d


# ---------------------------------------------------------------- tests


@pytest.mark.asyncio
async def test_no_degradation() -> None:
    """All sections complete — 0 degraded."""
    sections = [
        {"section_id": "s1", "section_order": 1, "title": "Theme 1"},
        {"section_id": "s2", "section_order": 2, "title": "Theme 2"},
    ]
    deps = _make_deps(sections)
    node = make_build_report_draft_node(deps)
    result = await node({"synthesis_result_id": str(uuid4())})

    assert result["section_count"] == 2
    assert result["degraded_section_count"] == 0
    assert len(result["sections"]) == 2
    for s in result["sections"]:
        assert s["section_status"] == "completed"
        assert s["draft_section_id"] is not None


@pytest.mark.asyncio
async def test_one_section_degraded() -> None:
    """One section fails → degraded; the other completes normally."""
    sections = [
        {"section_id": "s1", "section_order": 1, "title": "Theme 1"},
        {"section_id": "s2", "section_order": 2, "title": "Theme 2"},
    ]
    deps = _make_deps(sections, fail_ids={"s2"})
    node = make_build_report_draft_node(deps)
    result = await node({"synthesis_result_id": str(uuid4())})

    assert result["section_count"] == 2
    assert result["degraded_section_count"] == 1
    assert len(result["sections"]) == 2

    completed = [s for s in result["sections"] if s["section_status"] == "completed"]
    degraded = [s for s in result["sections"] if s["section_status"] == "degraded"]
    assert len(completed) == 1
    assert len(degraded) == 1
    assert completed[0]["section_id"] == "s1"
    assert completed[0]["draft_section_id"] is not None
    assert degraded[0]["section_id"] == "s2"
    assert degraded[0]["draft_section_id"] is None
    assert degraded[0]["degraded_reason"] == "model_unavailable"


@pytest.mark.asyncio
async def test_multiple_sections_degraded() -> None:
    """Two of three sections fail — 2 degraded, 1 completed."""
    sections = [
        {"section_id": "s1", "section_order": 1, "title": "Theme 1"},
        {"section_id": "s2", "section_order": 2, "title": "Theme 2"},
        {"section_id": "s3", "section_order": 3, "title": "Risks & Gaps"},
    ]
    deps = _make_deps(sections, fail_ids={"s2", "s3"})
    node = make_build_report_draft_node(deps)
    result = await node({"synthesis_result_id": str(uuid4())})

    assert result["section_count"] == 3
    assert result["degraded_section_count"] == 2
    assert len(result["sections"]) == 3

    completed = [s for s in result["sections"] if s["section_status"] == "completed"]
    degraded = [s for s in result["sections"] if s["section_status"] == "degraded"]
    assert len(completed) == 1
    assert len(degraded) == 2
    assert completed[0]["section_id"] == "s1"


@pytest.mark.asyncio
async def test_all_sections_degraded_raises() -> None:
    """All sections fail — assemble_report raises Stage5InvalidState."""
    sections = [
        {"section_id": "s1", "section_order": 1, "title": "Theme 1"},
        {"section_id": "s2", "section_order": 2, "title": "Theme 2"},
    ]
    deps = _make_deps(sections, fail_ids={"s1", "s2"})
    node = make_build_report_draft_node(deps)
    result = await node({"synthesis_result_id": str(uuid4())})

    assert result["degraded_section_count"] == 2

    # assemble_report with all degraded → raises
    assemble_node = make_assemble_report_node(deps)
    with pytest.raises(Exception) as excinfo:
        await assemble_node({
            "outline_id": str(uuid4()),
            "sections": result["sections"],
        })
    assert "degraded" in str(excinfo.value).lower()


@pytest.mark.asyncio
async def test_assemble_skips_degraded_sections() -> None:
    """Assemble report uses only completed sections."""
    sections = [
        {"section_id": "s1", "section_order": 1, "title": "Theme 1"},
        {"section_id": "s2", "section_order": 2, "title": "Theme 2"},
        {"section_id": "s3", "section_order": 3, "title": "Theme 3"},
    ]
    deps = _make_deps(sections, fail_ids={"s2"})
    build_node = make_build_report_draft_node(deps)
    build_result = await build_node({"synthesis_result_id": str(uuid4())})

    assemble_node = make_assemble_report_node(deps)
    assemble_result = await assemble_node({
        "outline_id": str(deps.report_outline_service._outline_id),
        "sections": build_result["sections"],
    })

    # assemble should have called report_service with 2 valid sections (not 3)
    assert assemble_result["assembled_section_count"] == 2
    assert assemble_result["degraded_section_count"] == 1
    assert assemble_result["report_id"] is not None

    # Verify report_service received only 2 draft_section_ids
    assert len(deps.report_service.calls) == 1
    assert len(deps.report_service.calls[0].draft_section_ids) == 2
