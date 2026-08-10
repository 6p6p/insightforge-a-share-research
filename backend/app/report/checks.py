"""Deterministic report checks (stage 5C, spec Q/R/S): 10 v1 checks, 0 LLM.

在 **verified Report**（`verify_report_integrity` 的 `VerifiedReport`）上运行。
每个 check 是纯函数：从 `CheckInput`（verified Outline + verified DraftSections +
report payload + Claim/Evidence 数据）读取，产出结构化 `CheckFinding` 列表
（**不存长 prose**，spec R）。

v1 checks（spec Q）：
1. `outline_section_coverage`——每个 Outline section exact once（缺 / 重 / 额外 /
   身份不一致 → finding）；
2. `draft_section_integrity`——所有 selected DraftSection verified（report section
   引用的 draft_section_id 必须指向本 section 已验证草稿且身份一致）；
3. `claim_reference_closure`——每个 report paragraph claim_id 属于对应 section
   allowed set（theme = outline section claim_ids；risks_and_gaps = 合成输入集）；
4. `evidence_reference_closure`——每个 evidence_card_id 真实绑定于段落引用的至少
   一个 Claim（risks/gaps policy：evidence 可空，但一旦引用必须绑定）；
5. `numeric_grounding`——**重新执行 Writer v2 numeric grounding**（复用
   `extract_quantitative_tokens`，与写入时同一套提取器）；
6. `forbidden_investment_language`——不得出现买入/卖出/目标价/收益承诺及 Writer
   已冻结词集（`contains_forbidden_language`）；
7. `internal_alias_leak`——正文不得出现 C1/E2/X1/G1 transport aliases
   （`find_inline_alias_leak`，与 Writer v2 同一正则）；
8. `conflict_gap_preservation`——Outline 指定的 conflict_indexes /
   evidence_gap_indexes 必须在对应 Report section 至少被一个 paragraph 显式引用
   （不能 Outline 有 gap 而 Writer 完全没写它）；
9. `empty_section`——每个 section 至少 1 paragraph；
10. `citation_provenance_closure`——所有 paragraph Evidence IDs 都可以
    Evidence → source provenance 真实追溯（只检查 closure，不重新 retrieval）。

`run_checks` 聚合全部 10 项并**确定性排序**（code + section_order + paragraph_index）
→ 相同输入永远相同 findings（spec T：deterministic findings order）。
"""

from dataclasses import dataclass
from uuid import UUID

from app.draft_section.contracts import VerifiedDraftSection, contains_forbidden_language
from app.draft_section.numeric import extract_quantitative_tokens
from app.draft_section.validate import find_inline_alias_leak
from app.report.contracts import CheckFinding
from app.report_outline.contracts import SECTION_TYPE_THEME, OutlineSection, VerifiedReportOutline


@dataclass(frozen=True)
class EvidenceCheckData:
    """一张 Evidence Card 的 check 用数据（短 DB session 加载，session 关闭后使用）。

    - bound_claim_ids：该证据真实绑定的 Claims（`claim_evidence_links`）；
    - has_provenance：Evidence → source provenance 是否真实可追溯（document_chunk
      有 source_id；macro_observation 有 observation_id）。
    """

    evidence_card_id: UUID
    evidence_statement: str
    quote_text: str | None
    origin_type: str
    has_provenance: bool
    bound_claim_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class CheckInput:
    """10 个确定性 check 的全部输入（verified artifacts + 加载的 Claim/Evidence 数据）。

    - claim_statements / evidence 按 **字符串 UUID** 键（与 report payload 的
      claim_ids / evidence_card_ids 一致，避免反复 parse）。
    """

    verified_outline: VerifiedReportOutline
    verified_drafts: dict[str, VerifiedDraftSection]  # section_id -> verified draft
    report_payload: dict  # {"sections": [...]} 按 Outline order
    claim_statements: dict[str, str]  # claim_id(str) -> statement
    evidence: dict[str, EvidenceCheckData]  # evidence_card_id(str) -> data


def run_checks(input_: CheckInput) -> list[CheckFinding]:
    """运行全部 10 个 v1 checks 并返回确定性排序的 findings。

    无 findings → 调用方判定 status='pass'；有任何 finding → 'fail'。**不自动修改
    Report**（spec R）。
    """
    findings: list[CheckFinding] = []
    findings.extend(_check_outline_section_coverage(input_))
    findings.extend(_check_draft_section_integrity(input_))
    findings.extend(_check_claim_reference_closure(input_))
    findings.extend(_check_evidence_reference_closure(input_))
    findings.extend(_check_numeric_grounding(input_))
    findings.extend(_check_forbidden_investment_language(input_))
    findings.extend(_check_internal_alias_leak(input_))
    findings.extend(_check_conflict_gap_preservation(input_))
    findings.extend(_check_empty_section(input_))
    findings.extend(_check_citation_provenance_closure(input_))
    return _sort_findings(input_, findings)


# ---------------------------------------------------------------- helpers


def _report_sections(payload: dict) -> list[dict]:
    sections = payload.get("sections")
    return sections if isinstance(sections, list) else []


def _allowed_claim_ids(
    verified_outline: VerifiedReportOutline, section: OutlineSection
) -> set[str]:
    """section 允许的 Claim 集（镜像 Writer v2 `_allowed_claim_ids`）。

    - theme section：只允许 outline 分配给该 theme 的 Claim；
    - risks_and_gaps section：不限制模型选择额外 Claims → 允许整个合成输入集。
    """
    if section.section_type == SECTION_TYPE_THEME:
        return {str(cid) for cid in section.claim_ids}
    return {str(cid) for cid in verified_outline.verified_synthesis_result.input_claim_ids}


def _sort_findings(input_: CheckInput, findings: list[CheckFinding]) -> list[CheckFinding]:
    """确定性排序（spec T）：(code, section_order, paragraph_index, related ids)。"""
    order_by_section: dict[str, int] = {}
    for section in _report_sections(input_.report_payload):
        if isinstance(section.get("section_order"), int):
            order_by_section[section["section_id"]] = section["section_order"]

    def key(finding: CheckFinding) -> tuple:
        return (
            finding.code,
            order_by_section.get(finding.section_id or "", 0),
            finding.paragraph_index if finding.paragraph_index is not None else -1,
            finding.related_claim_ids,
            finding.related_evidence_card_ids,
        )

    return sorted(findings, key=key)


# ---------------------------------------------------------------- 1-2 coverage / integrity


def _check_outline_section_coverage(input_: CheckInput) -> list[CheckFinding]:
    """每个 Outline section exact once；额外 / 身份不一致 → finding。"""
    findings: list[CheckFinding] = []
    outline_section_ids = {s.section_id for s in input_.verified_outline.sections}
    report_by_section: dict[str, list[dict]] = {}
    for section in _report_sections(input_.report_payload):
        report_by_section.setdefault(section["section_id"], []).append(section)

    for outline_section in input_.verified_outline.sections:
        matches = report_by_section.get(outline_section.section_id, [])
        if len(matches) != 1:
            findings.append(
                CheckFinding(code="outline_section_coverage", section_id=outline_section.section_id)
            )
            continue
        report_section = matches[0]
        identity = (
            report_section["section_order"] == outline_section.section_order
            and report_section["section_type"] == outline_section.section_type
            and report_section["title"] == outline_section.title
        )
        if not identity:
            findings.append(
                CheckFinding(code="outline_section_coverage", section_id=outline_section.section_id)
            )

    for section_id in report_by_section:
        if section_id not in outline_section_ids:
            findings.append(CheckFinding(code="outline_section_coverage", section_id=section_id))
    return findings


def _check_draft_section_integrity(input_: CheckInput) -> list[CheckFinding]:
    """所有 selected DraftSection verified（report section → 已验证草稿 + 身份一致）。"""
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        verified = input_.verified_drafts.get(section["section_id"])
        if verified is None:
            findings.append(
                CheckFinding(code="draft_section_integrity", section_id=section["section_id"])
            )
            continue
        identity = (
            section.get("draft_section_id") == str(verified.draft_section_id)
            and section["section_order"] == verified.section_order
            and section["section_type"] == verified.section_type
            and section["title"] == verified.title
        )
        if not identity:
            findings.append(
                CheckFinding(code="draft_section_integrity", section_id=section["section_id"])
            )
    return findings


# ---------------------------------------------------------------- 3-7 paragraph closure / policy


def _check_claim_reference_closure(input_: CheckInput) -> list[CheckFinding]:
    """每个 paragraph claim_id 必须属于对应 section allowed set。"""
    findings: list[CheckFinding] = []
    allowed_by_section: dict[str, set[str]] = {}
    for outline_section in input_.verified_outline.sections:
        allowed_by_section[outline_section.section_id] = _allowed_claim_ids(
            input_.verified_outline, outline_section
        )
    for section in _report_sections(input_.report_payload):
        allowed = allowed_by_section.get(section["section_id"], set())
        for p_index, paragraph in enumerate(section.get("paragraphs", [])):
            out_of_scope = [
                claim_id for claim_id in paragraph.get("claim_ids", []) if claim_id not in allowed
            ]
            if out_of_scope:
                findings.append(
                    CheckFinding(
                        code="claim_reference_closure",
                        section_id=section["section_id"],
                        paragraph_index=p_index,
                        related_claim_ids=tuple(out_of_scope),
                    )
                )
    return findings


def _check_evidence_reference_closure(input_: CheckInput) -> list[CheckFinding]:
    """每个 evidence_card_id 必须真实绑定于段落引用的至少一个 Claim。

    risks/gaps policy：evidence 可空，但一旦引用必须绑定（与 Writer v2 相同）。
    """
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        for p_index, paragraph in enumerate(section.get("paragraphs", [])):
            referenced_claims = set(paragraph.get("claim_ids", []))
            unbound: list[str] = []
            for evidence_id in paragraph.get("evidence_card_ids", []):
                item = input_.evidence.get(evidence_id)
                bound = {str(cid) for cid in item.bound_claim_ids} if item is not None else set()
                if item is None or not (bound & referenced_claims):
                    unbound.append(evidence_id)
            if unbound:
                findings.append(
                    CheckFinding(
                        code="evidence_reference_closure",
                        section_id=section["section_id"],
                        paragraph_index=p_index,
                        related_evidence_card_ids=tuple(unbound),
                    )
                )
    return findings


def _check_numeric_grounding(input_: CheckInput) -> list[CheckFinding]:
    """重新执行 Writer v2 numeric grounding（复用 `extract_quantitative_tokens`）。"""
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        for p_index, paragraph in enumerate(section.get("paragraphs", [])):
            grounding_texts: list[str] = []
            for claim_id in paragraph.get("claim_ids", []):
                statement = input_.claim_statements.get(claim_id)
                if statement is not None:
                    grounding_texts.append(statement)
            for evidence_id in paragraph.get("evidence_card_ids", []):
                item = input_.evidence.get(evidence_id)
                if item is not None:
                    grounding_texts.append(item.evidence_statement)
                    if item.quote_text:
                        grounding_texts.append(item.quote_text)
            corpus = "\n".join(grounding_texts)
            ungrounded = next(
                (
                    token
                    for token in extract_quantitative_tokens(paragraph.get("text", ""))
                    if token not in corpus
                ),
                None,
            )
            if ungrounded is not None:
                findings.append(
                    CheckFinding(
                        code="numeric_grounding",
                        section_id=section["section_id"],
                        paragraph_index=p_index,
                        related_claim_ids=tuple(paragraph.get("claim_ids", [])),
                        related_evidence_card_ids=tuple(paragraph.get("evidence_card_ids", [])),
                    )
                )
    return findings


def _check_forbidden_investment_language(input_: CheckInput) -> list[CheckFinding]:
    """不得出现买入/卖出/目标价/收益承诺及 Writer 已冻结词集。"""
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        for p_index, paragraph in enumerate(section.get("paragraphs", [])):
            if contains_forbidden_language(paragraph.get("text", "")) is not None:
                findings.append(
                    CheckFinding(
                        code="forbidden_investment_language",
                        section_id=section["section_id"],
                        paragraph_index=p_index,
                    )
                )
    return findings


def _check_internal_alias_leak(input_: CheckInput) -> list[CheckFinding]:
    """正文不得出现 C/E/X/G transport aliases（与 Writer v2 同一正则）。"""
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        for p_index, paragraph in enumerate(section.get("paragraphs", [])):
            if find_inline_alias_leak(paragraph.get("text", "")) is not None:
                findings.append(
                    CheckFinding(
                        code="internal_alias_leak",
                        section_id=section["section_id"],
                        paragraph_index=p_index,
                    )
                )
    return findings


# ---------------------------------------------------------------- 8-10 preservation / closure


def _check_conflict_gap_preservation(input_: CheckInput) -> list[CheckFinding]:
    """Outline 指定的 conflict/gap indexes 必须在对应 section 被显式引用。"""
    findings: list[CheckFinding] = []
    report_by_section = {s["section_id"]: s for s in _report_sections(input_.report_payload)}
    for outline_section in input_.verified_outline.sections:
        report_section = report_by_section.get(outline_section.section_id)
        if report_section is None:
            continue  # outline_section_coverage check 已标记
        referenced_conflicts: set[int] = set()
        referenced_gaps: set[int] = set()
        for paragraph in report_section.get("paragraphs", []):
            referenced_conflicts.update(paragraph.get("conflict_indexes", []))
            referenced_gaps.update(paragraph.get("evidence_gap_indexes", []))
        if any(index not in referenced_conflicts for index in outline_section.conflict_indexes):
            findings.append(
                CheckFinding(
                    code="conflict_gap_preservation", section_id=outline_section.section_id
                )
            )
        if any(index not in referenced_gaps for index in outline_section.evidence_gap_indexes):
            findings.append(
                CheckFinding(
                    code="conflict_gap_preservation", section_id=outline_section.section_id
                )
            )
    return findings


def _check_empty_section(input_: CheckInput) -> list[CheckFinding]:
    """每个 section 至少 1 paragraph。"""
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        if not section.get("paragraphs"):
            findings.append(CheckFinding(code="empty_section", section_id=section["section_id"]))
    return findings


def _check_citation_provenance_closure(input_: CheckInput) -> list[CheckFinding]:
    """所有 paragraph Evidence IDs 都可 Evidence → source provenance 真实追溯。"""
    findings: list[CheckFinding] = []
    for section in _report_sections(input_.report_payload):
        for p_index, paragraph in enumerate(section.get("paragraphs", [])):
            missing = [
                evidence_id
                for evidence_id in paragraph.get("evidence_card_ids", [])
                if (item := input_.evidence.get(evidence_id)) is None or not item.has_provenance
            ]
            if missing:
                findings.append(
                    CheckFinding(
                        code="citation_provenance_closure",
                        section_id=section["section_id"],
                        paragraph_index=p_index,
                        related_evidence_card_ids=tuple(missing),
                    )
                )
    return findings
