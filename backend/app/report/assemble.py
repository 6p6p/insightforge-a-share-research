"""Deterministic report assembly (stage 5C, spec K/L/M): pure functions, 0 LLM.

`assemble_report_payload` 把**已全部 verify 通过**的 Outline + DraftSections 按
Outline order 机械拼装为 v1 report payload，并强制执行 coverage / identity 硬边界
（spec K）：

- 每个 DraftSection 的 `outline_id` 必须等于 input outline；
- 每个 Outline section **恰好一个** DraftSection（missing / duplicate / extra 全部
  拒绝）；
- section_id / order / type / title 必须与 Outline **完全一致**（不重写）；
- sections 严格按 Outline order（spec M），不重新生成 summary / conclusion /
  investment recommendation。

纯函数（DB session 已关闭后调用）；损坏 → `ReportAssemblyError`（0 写）。
"""

from dataclasses import dataclass
from uuid import UUID

from app.draft_section.contracts import VerifiedDraftSection
from app.report.errors import ReportAssemblyError, ReportIntegrityError
from app.report_outline.contracts import VerifiedReportOutline


@dataclass(frozen=True)
class AssembledSectionDraft:
    """一个已验证 DraftSection + 它的 persisted section_payload（供拼装正文）。"""

    verified: VerifiedDraftSection
    section_payload: dict


def assemble_report_payload(
    *,
    verified_outline: VerifiedReportOutline,
    drafts: list[AssembledSectionDraft],
) -> dict:
    """把 verified Outline + verified DraftSections 拼装为 v1 report payload。

    coverage / identity 硬边界（spec K）任一违反 → `ReportAssemblyError`（0 写）。

    不设「数量」快速守卫：每个 Outline section 恰好一个 DraftSection 由具体的
    missing / duplicate / extra 检查精确表达（数量不等必然命中其一），让错误信息
    精确到缺陷本身。
    """
    by_section_id: dict[str, AssembledSectionDraft] = {}
    for draft in drafts:
        section_id = draft.verified.section_id
        if section_id in by_section_id:
            raise ReportAssemblyError(f"duplicate draft section for {section_id!r}")
        by_section_id[section_id] = draft

    outline_section_ids = {section.section_id for section in verified_outline.sections}
    for section_id in by_section_id:
        if section_id not in outline_section_ids:
            raise ReportAssemblyError(f"extra draft section for unknown section {section_id!r}")

    sections: list[dict] = []
    for section in verified_outline.sections:
        draft = by_section_id.get(section.section_id)
        if draft is None:
            raise ReportAssemblyError(
                f"missing draft section for outline section {section.section_id!r}"
            )
        verified = draft.verified
        identity_checks = [
            (verified.outline_id, verified_outline.outline_id, "outline_id"),
            (verified.section_order, section.section_order, "section_order"),
            (verified.section_type, section.section_type, "section_type"),
            (verified.title, section.title, "title"),
        ]
        for actual, want, field in identity_checks:
            if actual != want:
                raise ReportAssemblyError(
                    f"draft section {verified.draft_section_id} {field} mismatch "
                    f"with outline section {section.section_id!r}"
                )
        paragraphs = draft.section_payload.get("paragraphs")
        if not isinstance(paragraphs, list) or not paragraphs:
            raise ReportAssemblyError(
                f"draft section {verified.draft_section_id} payload has no paragraphs"
            )
        sections.append(
            {
                "section_id": section.section_id,
                "section_order": section.section_order,
                "section_type": section.section_type,
                "title": section.title,
                "draft_section_id": str(verified.draft_section_id),
                "paragraphs": paragraphs,
            }
        )
    return {"sections": sections}


def draft_section_fingerprint_data(
    drafts: list[AssembledSectionDraft],
) -> list[dict]:
    """selected draft sections 的 canonical 指纹数据（按 section_order，spec N）。

    供 `compute_report_fingerprint` 使用：每个 DraftSection 的
    draft_section_id / section_fingerprint / writer_name / writer_version /
    writer_model_id。按 section_order 排序（装配已验证 order 与 Outline 一致）。
    """
    ordered = sorted(drafts, key=lambda item: item.verified.section_order)
    return [
        {
            "section_order": verified.section_order,
            "draft_section_id": str(verified.draft_section_id),
            "section_fingerprint": verified.section_fingerprint,
            "writer_name": verified.writer_name,
            "writer_version": verified.writer_version,
            "writer_model_id": verified.writer_model_id,
        }
        for verified in (item.verified for item in ordered)
    ]


def extract_draft_section_ids(payload: dict) -> tuple[UUID, ...]:
    """从 persisted report payload 提取 selected draft_section_id（按 section order）。

    供 `verify_report_integrity` 使用（先加载既有行 → 提取 draft ids → 逐个 verify
    → rebuild exact payload → 与 persisted 对比）。payload 结构损坏 →
    `ReportIntegrityError`（**不自动 repair**）。
    """
    sections = payload.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ReportIntegrityError("report payload sections must be a non-empty list")
    ids: list[UUID] = []
    for index, section in enumerate(sections):
        if not isinstance(section, dict):
            raise ReportIntegrityError(f"report payload section[{index}] must be an object")
        raw = section.get("draft_section_id")
        if isinstance(raw, bool) or not isinstance(raw, str):
            raise ReportIntegrityError(f"report payload section[{index}] draft_section_id invalid")
        try:
            ids.append(UUID(raw))
        except ValueError:
            raise ReportIntegrityError(
                f"report payload section[{index}] draft_section_id not a UUID"
            ) from None
    return tuple(ids)
