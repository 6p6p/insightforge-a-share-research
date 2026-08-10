"""Deterministic report contracts tests (stage 5C, spec N/S/J/M): 纯函数（无 DB / 0 LLM）。

覆盖：
- `compute_report_fingerprint`：确定性 + 敏感（schema / outline / draft sections /
  payload 任一变化 → 新指纹）；**不含** report_id / created_at（结构上无法入参）；
- `compute_check_fingerprint`：确定性 + 敏感（schema / report_id / report_fingerprint
  / findings）；
- `ReportAssemblyDraft` 构造校验（outline_id / draft_section_ids exact）；
- `CheckFinding.to_dict`：只含非空字段（可空字段按需要）。
"""

from datetime import date
from uuid import UUID, uuid4

import pytest

from app.report.contracts import (
    REPORT_CHECK_SCHEMA_VERSION,
    REPORT_SCHEMA_VERSION,
    CheckFinding,
    ReportAssemblyDraft,
    compute_check_fingerprint,
    compute_report_fingerprint,
)
from app.report.errors import ReportInputError

_AS_OF = date(2026, 8, 10)


def _fingerprint_args(**overrides) -> dict:
    args = dict(
        report_schema_version=REPORT_SCHEMA_VERSION,
        outline_id=uuid4(),
        outline_fingerprint="c" * 64,
        company_id=uuid4(),
        research_question_sha256="b" * 64,
        analysis_as_of=_AS_OF,
        draft_sections=[
            {
                "section_order": 1,
                "draft_section_id": str(uuid4()),
                "section_fingerprint": "e" * 64,
                "writer_name": "evidence_bound_section_writer",
                "writer_version": 2,
                "writer_model_id": "deepseek:deepseek-v4-flash",
            }
        ],
        report_payload={"sections": [{"section_id": "S1", "section_order": 1}]},
    )
    args.update(overrides)
    return args


# ---------------------------------------------------------------- report fingerprint


def test_report_fingerprint_deterministic_sha256() -> None:
    args = _fingerprint_args()
    first = compute_report_fingerprint(**args)
    second = compute_report_fingerprint(**args)
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


def test_report_fingerprint_sensitive_to_all_derived_fields() -> None:
    base = _fingerprint_args()
    fp = compute_report_fingerprint(**base)
    changes = {
        "report_schema_version": 2,
        "outline_id": uuid4(),
        "outline_fingerprint": "d" * 64,
        "company_id": uuid4(),
        "research_question_sha256": "a" * 64,
        "analysis_as_of": date(2026, 8, 11),
    }
    for field, value in changes.items():
        changed = compute_report_fingerprint(**{**base, field: value})
        assert changed != fp, f"fingerprint insensitive to {field}"


def test_report_fingerprint_sensitive_to_draft_sections() -> None:
    base = _fingerprint_args()
    fp = compute_report_fingerprint(**base)
    changed_draft_id = compute_report_fingerprint(
        **{
            **base,
            "draft_sections": [{**base["draft_sections"][0], "draft_section_id": str(uuid4())}],
        }
    )
    assert changed_draft_id != fp
    changed_section_fp = compute_report_fingerprint(
        **{
            **base,
            "draft_sections": [{**base["draft_sections"][0], "section_fingerprint": "f" * 64}],
        }
    )
    assert changed_section_fp != fp
    changed_writer = compute_report_fingerprint(
        **{
            **base,
            "draft_sections": [{**base["draft_sections"][0], "writer_model_id": "other:model"}],
        }
    )
    assert changed_writer != fp


def test_report_fingerprint_sensitive_to_payload() -> None:
    base = _fingerprint_args()
    fp = compute_report_fingerprint(**base)
    changed_payload = compute_report_fingerprint(
        **{
            **base,
            "report_payload": {"sections": [{"section_id": "S1", "section_order": 1, "x": 1}]},
        }
    )
    assert changed_payload != fp


# ---------------------------------------------------------------- check fingerprint


def _check_args(**overrides) -> dict:
    args = dict(
        check_schema_version=REPORT_CHECK_SCHEMA_VERSION,
        report_id=uuid4(),
        report_fingerprint="f" * 64,
        findings=[{"code": "empty_section", "section_id": "S1"}],
    )
    args.update(overrides)
    return args


def test_check_fingerprint_deterministic_sha256() -> None:
    args = _check_args()
    first = compute_check_fingerprint(**args)
    second = compute_check_fingerprint(**args)
    assert first == second
    assert len(first) == 64
    assert all(c in "0123456789abcdef" for c in first)


def test_check_fingerprint_sensitive_to_all_fields() -> None:
    base = _check_args()
    fp = compute_check_fingerprint(**base)
    changes = {
        "check_schema_version": 2,
        "report_id": uuid4(),
        "report_fingerprint": "e" * 64,
        "findings": [{"code": "empty_section", "section_id": "S2"}],
    }
    for field, value in changes.items():
        changed = compute_check_fingerprint(**{**base, field: value})
        assert changed != fp, f"check fingerprint insensitive to {field}"


# ---------------------------------------------------------------- ReportAssemblyDraft


def _draft_ids(*ids) -> tuple[UUID, ...]:
    return tuple(ids if ids else (uuid4(), uuid4()))


def test_assembly_draft_accepts_valid_input() -> None:
    draft = ReportAssemblyDraft(outline_id=uuid4(), draft_section_ids=_draft_ids())
    assert isinstance(draft.outline_id, UUID)
    assert len(draft.draft_section_ids) == 2


def test_assembly_draft_rejects_non_uuid_outline() -> None:
    with pytest.raises(ReportInputError):
        ReportAssemblyDraft(outline_id="not-a-uuid", draft_section_ids=_draft_ids())


def test_assembly_draft_rejects_empty_selection() -> None:
    with pytest.raises(ReportInputError):
        ReportAssemblyDraft(outline_id=uuid4(), draft_section_ids=())


def test_assembly_draft_rejects_non_uuid_member() -> None:
    with pytest.raises(ReportInputError):
        ReportAssemblyDraft(outline_id=uuid4(), draft_section_ids=(uuid4(), "x"))


def test_assembly_draft_rejects_duplicate_selection() -> None:
    same = uuid4()
    with pytest.raises(ReportInputError):
        ReportAssemblyDraft(outline_id=uuid4(), draft_section_ids=(same, same))


# ---------------------------------------------------------------- CheckFinding.to_dict


def test_check_finding_to_dict_omits_empty_fields() -> None:
    assert CheckFinding(code="empty_section").to_dict() == {"code": "empty_section"}


def test_check_finding_to_dict_includes_set_fields() -> None:
    finding = CheckFinding(
        code="claim_reference_closure",
        section_id="S2",
        paragraph_index=0,
        related_claim_ids=("a", "b"),
    )
    assert finding.to_dict() == {
        "code": "claim_reference_closure",
        "section_id": "S2",
        "paragraph_index": 0,
        "related_claim_ids": ["a", "b"],
    }
