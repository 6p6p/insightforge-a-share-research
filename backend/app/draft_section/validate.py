"""Hard provenance + numeric + policy validation (stage 5B, spec J/K/L/M/N; Gate 0 A/B).

确定性代码在 LLM 调用结束后、持久化之前，对 `WriterDecision` 逐段执行：

- **schema**：`_check_ref_format`（C/E/X/G<number>）→ 格式非法 = MalformedOutput；
- **Section-aware paragraph contract**（spec B）：theme 每段 claim_refs >= 1 且
  evidence_refs >= 1；risks_and_gaps 每段至少引用 claim_ref / conflict_ref /
  gap_ref 之一（evidence 可空）——违反 → `DraftSectionParagraphContract`；
- **inline alias leak policy**（spec A）：paragraph.text 禁止出现任何合法
  C/E/X/G<number> transport alias token（如「（C3）」「见G1」）——违反 →
  `DraftSectionInlineAliasLeak`；
- **known / cross-section**（spec K）：每个 claim_ref 必须属于当前 section 允许
  Claim set；引用合成输入集存在但不在本 section 的 Claim（编号在
  `1..total_claim_count` 内却不在 section pack）= CrossSection；超出合成输入集 =
  Unknown。Evidence / X / G ref 必须在本 section pack 内，否则 Unknown；
- **unbound evidence**（spec K）：每个 evidence_ref 必须真实绑定于段落引用的
  至少一个 Claim（禁止「引用只属于其他 C 的 E」）；
- **numeric grounding**（spec L）：段落的 quantitative tokens 必须逐字出现在所
  引用 Claim/Evidence 的 statement / quote 中；evidence_refs 为空时只能从
  referenced Claim statement 获得 grounding（Conflict/Gap 文本不是新数字来源）；
- **forbidden language**（spec M）：段落不得含买入/卖出建议、目标价、收益承诺。

`resolve_decision` 在验证通过后把 alias / index 解析回真实 UUID / index，产出
persisted payload（v1 = `{"paragraphs":[...]}`，**只存真实 ID，不存 alias**）。

`verify_resolved_payload` 供 replay 校验：解析 persisted payload 并验证全部
persisted Claim/Evidence ID 属于本 section allowed 集、index 在范围内——损坏 →
`DraftSectionIntegrityError`（**不自动 repair**）。

`verify_payload_contracts` 供 public integrity verify：解析 persisted payload →
重构 alias refs → 逐段重跑完整 Section-aware 校验（scope / contract / inline
alias / unbound / numeric / forbidden）。
"""

import re

from app.draft_section.contracts import (
    ParagraphCandidate,
    WriterDecision,
    contains_forbidden_language,
    valid_ref_format,
)
from app.draft_section.errors import (
    DraftSectionCrossSectionRef,
    DraftSectionForbiddenLanguage,
    DraftSectionInlineAliasLeak,
    DraftSectionIntegrityError,
    DraftSectionMalformedOutput,
    DraftSectionParagraphContract,
    DraftSectionUnboundEvidence,
    DraftSectionUnknownRef,
)
from app.draft_section.numeric import assert_numeric_grounding
from app.draft_section.packs import SectionInputPack
from app.report_outline.contracts import SECTION_TYPE_THEME

# 内联 alias leak policy（spec A）：正文禁止出现任何合法 C/E/X/G<number>
# transport alias token。复用 numeric.py 已证明正确的 ASCII lookaround（不能用
# `\b`：中文是 `\w`）；IGNORECASE 与 numeric 剥离语义一致（凡 numeric 当作标签
# 剥离的，leak policy 同样拒绝）。合法 alias 是 C/E/X/G + 数字（同 valid_ref_format
# 的 prefix 语义）。「维生素C1」这类真实文本含 C/E/X/G+数字 会被命中——spec A 明确
# 不要为理论边角过度复杂化，保守拒绝。
_INLINE_ALIAS_REF = re.compile(r"(?<![A-Za-z0-9_])[CEXG][1-9]\d*(?![A-Za-z0-9_])", re.IGNORECASE)


def find_inline_alias_leak(text: str) -> str | None:
    """返回 text 中第一个内联 alias token；未命中返回 None。"""
    match = _INLINE_ALIAS_REF.search(text)
    return match.group(0) if match is not None else None


def _check_ref_format(ref: str, prefix: str) -> None:
    if not valid_ref_format(ref, prefix):
        raise DraftSectionMalformedOutput(f"invalid {prefix}-ref format: {ref!r}")


def validate_decision(
    *,
    pack: SectionInputPack,
    decision: WriterDecision,
    total_claim_count: int,
) -> None:
    """hard provenance + numeric + policy 校验（LLM 调用结束后，持久化前）。

    `total_claim_count` = 合成输入集大小（`VerifiedSynthesisResult.input_claim_ids`），
    用于区分 Unknown（编号超合成集）与 CrossSection（编号在合成集但不在本 section）。
    """
    claim_by_alias = {item.alias: item for item in pack.claims}
    evidence_by_alias = {item.alias: item for item in pack.evidence}
    conflict_aliases = {item.alias for item in pack.conflicts}
    gap_aliases = {item.alias for item in pack.gaps}

    for index, paragraph in enumerate(decision.paragraphs):
        _validate_paragraph(
            paragraph=paragraph,
            index=index,
            claim_by_alias=claim_by_alias,
            evidence_by_alias=evidence_by_alias,
            conflict_aliases=conflict_aliases,
            gap_aliases=gap_aliases,
            total_claim_count=total_claim_count,
            section_type=pack.section_type,
        )


def _validate_paragraph(
    *,
    paragraph: ParagraphCandidate,
    index: int,
    claim_by_alias: dict,
    evidence_by_alias: dict,
    conflict_aliases: set[str],
    gap_aliases: set[str],
    total_claim_count: int,
    section_type: str,
) -> None:
    for ref in paragraph.claim_refs:
        _check_ref_format(ref, "C")
        if ref in claim_by_alias:
            continue
        number = int(ref[1:])
        if number <= total_claim_count:
            raise DraftSectionCrossSectionRef(
                f"paragraph[{index}] references claim {ref} outside this section"
            )
        raise DraftSectionUnknownRef(f"paragraph[{index}] references unknown claim alias {ref}")

    for ref in paragraph.evidence_refs:
        _check_ref_format(ref, "E")
        if ref not in evidence_by_alias:
            raise DraftSectionUnknownRef(
                f"paragraph[{index}] references unknown evidence alias {ref}"
            )

    for ref in paragraph.conflict_refs:
        _check_ref_format(ref, "X")
        if ref not in conflict_aliases:
            raise DraftSectionUnknownRef(
                f"paragraph[{index}] references unknown conflict alias {ref}"
            )

    for ref in paragraph.gap_refs:
        _check_ref_format(ref, "G")
        if ref not in gap_aliases:
            raise DraftSectionUnknownRef(
                f"paragraph[{index}] references unknown evidence gap alias {ref}"
            )

    # Section-aware paragraph contract（spec B）：真正 required policy 由
    # section_type 决定（Pydantic 只保证 list + text 非空）。
    if section_type == SECTION_TYPE_THEME:
        if not paragraph.claim_refs:
            raise DraftSectionParagraphContract(
                f"paragraph[{index}] theme section requires at least 1 claim_ref"
            )
        if not paragraph.evidence_refs:
            raise DraftSectionParagraphContract(
                f"paragraph[{index}] theme section requires at least 1 evidence_ref"
            )
    elif not (paragraph.claim_refs or paragraph.conflict_refs or paragraph.gap_refs):
        # risks_and_gaps：至少引用 claim / conflict / gap 之一（evidence 可空）。
        raise DraftSectionParagraphContract(
            f"paragraph[{index}] risks_and_gaps section requires at least one of "
            "claim_ref / conflict_ref / gap_ref"
        )

    # inline alias leak policy（spec A）：正文不得含 C/E/X/G transport alias。
    leak = find_inline_alias_leak(paragraph.text)
    if leak is not None:
        raise DraftSectionInlineAliasLeak(
            f"paragraph[{index}] leaks inline transport alias: {leak!r}"
        )

    referenced_claims = set(paragraph.claim_refs)
    for ref in paragraph.evidence_refs:
        if not (set(evidence_by_alias[ref].claim_aliases) & referenced_claims):
            raise DraftSectionUnboundEvidence(
                f"paragraph[{index}] evidence {ref} is not bound to any referenced claim"
            )

    # numeric grounding（spec L / B）：evidence_refs 为空时只能从 referenced
    # Claim statement 获得 grounding——Conflict / Gap 文本不是新数字来源。
    grounding_texts: list[str] = []
    for ref in paragraph.claim_refs:
        grounding_texts.append(claim_by_alias[ref].statement)
    for ref in paragraph.evidence_refs:
        item = evidence_by_alias[ref]
        grounding_texts.append(item.evidence_statement)
        if item.quote_text:
            grounding_texts.append(item.quote_text)
    assert_numeric_grounding(paragraph_text=paragraph.text, grounding_texts=grounding_texts)

    forbidden = contains_forbidden_language(paragraph.text)
    if forbidden is not None:
        raise DraftSectionForbiddenLanguage(
            f"paragraph[{index}] contains forbidden investment language: {forbidden}"
        )


def resolve_decision(pack: SectionInputPack, decision: WriterDecision) -> dict:
    """验证通过后把 alias / index 解析回真实 ID，产出规范化 persisted payload。

    payload（v1）：
    ```
    {"paragraphs": [{"text":..., "claim_ids":[...], "evidence_card_ids":[...],
                     "conflict_indexes":[...], "evidence_gap_indexes":[...]}]}
    ```
    只存真实 claim_id / evidence_card_id（字符串 UUID）与 conflict/gap index；
    **不存 alias / prompt / raw provider response**。
    """
    claim_map = pack.claim_alias_map()
    evidence_map = pack.evidence_alias_map()
    conflict_index_by_alias = {item.alias: index for index, item in enumerate(pack.conflicts)}
    gap_index_by_alias = {item.alias: index for index, item in enumerate(pack.gaps)}

    paragraphs: list[dict] = []
    for paragraph in decision.paragraphs:
        paragraphs.append(
            {
                "text": paragraph.text,
                "claim_ids": _dedupe([str(claim_map[ref]) for ref in paragraph.claim_refs]),
                "evidence_card_ids": _dedupe(
                    [str(evidence_map[ref]) for ref in paragraph.evidence_refs]
                ),
                "conflict_indexes": _dedupe(
                    [conflict_index_by_alias[ref] for ref in paragraph.conflict_refs]
                ),
                "evidence_gap_indexes": _dedupe(
                    [gap_index_by_alias[ref] for ref in paragraph.gap_refs]
                ),
            }
        )
    return {"paragraphs": paragraphs}


def verify_resolved_payload(pack: SectionInputPack, payload: dict) -> None:
    """replay / integrity 校验：解析 persisted payload，验证全部 ID / index 属于 allowed 集。

    **只检查 scope / 结构**（ID 属 allowed 集、index 在范围内、段落非空）；段落
    required policy（theme 需 C+E / risks_and_gaps 需 C/X/G 之一）由
    `verify_payload_contracts` / `_validate_paragraph` 决定（spec B：允许空 ref）。
    损坏 → `DraftSectionIntegrityError`，**不自动 repair**。text 篡改由
    section_fingerprint 重算捕获（见 service `_verify_replay`）。
    """
    paragraphs = payload.get("paragraphs")
    if not isinstance(paragraphs, list) or not paragraphs:
        raise DraftSectionIntegrityError(
            "draft section payload paragraphs must be a non-empty list"
        )

    allowed_claims = {str(cid) for cid in pack.claim_alias_map().values()}
    allowed_evidence = {str(cid) for cid in pack.evidence_alias_map().values()}
    max_conflict_index = len(pack.conflicts) - 1
    max_gap_index = len(pack.gaps) - 1

    for index, paragraph in enumerate(paragraphs):
        if not isinstance(paragraph, dict):
            raise DraftSectionIntegrityError(f"draft section paragraph[{index}] must be an object")
        text = paragraph.get("text")
        if not isinstance(text, str) or not text.strip():
            raise DraftSectionIntegrityError(f"draft section paragraph[{index}] text invalid")
        claim_ids = paragraph.get("claim_ids")
        evidence_ids = paragraph.get("evidence_card_ids")
        if not isinstance(claim_ids, list):
            raise DraftSectionIntegrityError(f"draft section paragraph[{index}] claim_ids invalid")
        if not isinstance(evidence_ids, list):
            raise DraftSectionIntegrityError(
                f"draft section paragraph[{index}] evidence_card_ids invalid"
            )
        for raw in claim_ids:
            if not isinstance(raw, str) or raw not in allowed_claims:
                raise DraftSectionIntegrityError(
                    f"draft section paragraph[{index}] claim_id not in allowed set"
                )
        for raw in evidence_ids:
            if not isinstance(raw, str) or raw not in allowed_evidence:
                raise DraftSectionIntegrityError(
                    f"draft section paragraph[{index}] evidence_card_id not in allowed set"
                )
        _check_indexes(paragraph.get("conflict_indexes", []), max_conflict_index, "conflict", index)
        _check_indexes(
            paragraph.get("evidence_gap_indexes", []), max_gap_index, "evidence_gap", index
        )


def reconstruct_paragraphs(pack: SectionInputPack, payload: dict) -> list[ParagraphCandidate]:
    """把 persisted payload（真实 ID / index）重构为 alias 级 ParagraphCandidate。

    供 `verify_payload_contracts` 复用与写入时完全一致的 `_validate_paragraph`。
    前提：ID / index 已通过 `verify_resolved_payload` scope 校验（均在 allowed 集），
    因此反向映射是良定义；异常 ID 会触发 KeyError → 调用方捕获为 integrity 错误。
    """
    claim_alias_by_id = {str(cid): alias for alias, cid in pack.claim_alias_map().items()}
    evidence_alias_by_id = {str(cid): alias for alias, cid in pack.evidence_alias_map().items()}
    conflict_alias_by_index = {index: item.alias for index, item in enumerate(pack.conflicts)}
    gap_alias_by_index = {index: item.alias for index, item in enumerate(pack.gaps)}

    paragraphs: list[ParagraphCandidate] = []
    for raw in payload["paragraphs"]:
        paragraphs.append(
            ParagraphCandidate(
                text=raw["text"],
                claim_refs=[claim_alias_by_id[cid] for cid in raw["claim_ids"]],
                evidence_refs=[evidence_alias_by_id[cid] for cid in raw["evidence_card_ids"]],
                conflict_refs=[conflict_alias_by_index[i] for i in raw.get("conflict_indexes", [])],
                gap_refs=[gap_alias_by_index[i] for i in raw.get("evidence_gap_indexes", [])],
            )
        )
    return paragraphs


def verify_payload_contracts(
    pack: SectionInputPack, payload: dict, *, total_claim_count: int
) -> None:
    """public integrity verify：scope + 完整 Section-aware 契约重验证。

    - `verify_resolved_payload`：结构 / ID scope / index 范围；
    - `reconstruct_paragraphs` → `_validate_paragraph`：paragraph contract（theme
      C+E / risks_and_gaps C/X/G）、inline alias leak、unbound、numeric grounding、
      forbidden language。

    损坏 → 对应 `DraftSectionError`（不自动 repair）。写入时已通过
    `validate_decision` 的稿件重放本函数，保证与写入时同一套校验。
    """
    verify_resolved_payload(pack, payload)
    claim_by_alias = {item.alias: item for item in pack.claims}
    evidence_by_alias = {item.alias: item for item in pack.evidence}
    conflict_aliases = {item.alias for item in pack.conflicts}
    gap_aliases = {item.alias for item in pack.gaps}
    for index, paragraph in enumerate(reconstruct_paragraphs(pack, payload)):
        _validate_paragraph(
            paragraph=paragraph,
            index=index,
            claim_by_alias=claim_by_alias,
            evidence_by_alias=evidence_by_alias,
            conflict_aliases=conflict_aliases,
            gap_aliases=gap_aliases,
            total_claim_count=total_claim_count,
            section_type=pack.section_type,
        )


def _check_indexes(value, maximum: int, label: str, paragraph_index: int) -> None:
    if not isinstance(value, list):
        raise DraftSectionIntegrityError(
            f"draft section paragraph[{paragraph_index}] {label}_indexes must be a list"
        )
    for raw in value:
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0 or raw > maximum:
            raise DraftSectionIntegrityError(
                f"draft section paragraph[{paragraph_index}] {label}_index out of range"
            )


def _dedupe(values: list) -> list:
    seen: set = set()
    ordered: list = []
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered
