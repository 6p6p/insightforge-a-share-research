"""Structured claim synthesis contracts unit tests (stage 4D.1B).

纯函数校验（无 DB / 无网络 / 无 LLM）：
- SynthesisAnalysisRequest / Context 构造校验（synthesis_id 必须 UUID）；
- schema 层局部规则（text trim 非空、C ref 格式、组大小、canonical 在组内、
  themes ≥1、claim_roles 非空）；
- validate_synthesis_output：全部 C refs 已知（unknown → UnknownRef）+ no-cherry-
  picking 硬边界（claim_roles 恰好覆盖每条 input Claim 一次，缺漏/重复 →
  NoCherryPicking）；
- compute_synthesis_result_fingerprint 确定性（同 input → 同 fp；output /
  analyst_model_id / synthesis_fingerprint / schema_version 任一变化 → 新 fp；
  不含 synthesis_id / created_at）。
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analysis.synthesis.contracts import (
    SYNTHESIS_ANALYST_NAME,
    SYNTHESIS_ANALYST_VERSION,
    SYNTHESIS_RESULT_SCHEMA_VERSION,
    SynthesisAnalysisContext,
    SynthesisAnalysisOutput,
    SynthesisAnalysisRequest,
    SynthesisClaimRole,
    SynthesisClaimRoleAssignment,
    SynthesisConflict,
    SynthesisDuplicate,
    SynthesisEvidenceGap,
    SynthesisPriority,
    SynthesisSeverity,
    SynthesisTheme,
    compute_synthesis_result_fingerprint,
    validate_synthesis_output,
)
from app.analysis.synthesis.errors import (
    SynthesisAnalysisInputError,
    SynthesisAnalysisNoCherryPicking,
    SynthesisAnalysisUnknownRef,
)

_CUTOFF = __import__("datetime").date(2026, 8, 10)


def _roles(*refs: str) -> list[SynthesisClaimRoleAssignment]:
    return [
        SynthesisClaimRoleAssignment(
            claim_ref=ref,
            role=SynthesisClaimRole.SUPPORT,
            rationale=f"支持 {ref}",
        )
        for ref in refs
    ]


def _valid_output(*claim_refs: str) -> SynthesisAnalysisOutput:
    refs = list(claim_refs) or ["C1", "C2", "C3", "C4"]
    return SynthesisAnalysisOutput(
        summary="贵州茅台综合判断：营收增长确定性较高，但估值偏高存在压力。",
        themes=[
            SynthesisTheme(
                title="营收增长确定",
                summary="多角度证据支持营收增长。",
                claim_refs=refs,
            )
        ],
        claim_roles=_roles(*refs),
        duplicates=[],
        conflicts=[],
        evidence_gaps=[
            SynthesisEvidenceGap(
                description="缺少现金流证据",
                claim_refs=refs[:1],
                suggested_evidence="经营现金流数据",
                priority=SynthesisPriority.MEDIUM,
            )
        ],
    )


# ---------------------------------------------------------------- request / context


class TestSynthesisAnalysisRequest:
    def test_accepts_uuid(self) -> None:
        request = SynthesisAnalysisRequest(synthesis_id=uuid4())
        assert request.synthesis_id is not None

    def test_rejects_non_uuid(self) -> None:
        with pytest.raises(SynthesisAnalysisInputError):
            SynthesisAnalysisRequest(synthesis_id="not-a-uuid")

    def test_rejects_bool(self) -> None:
        with pytest.raises(SynthesisAnalysisInputError):
            SynthesisAnalysisRequest(synthesis_id=True)


class TestSynthesisAnalysisContext:
    def test_constructs(self) -> None:
        context = SynthesisAnalysisContext(
            research_question="贵州茅台2026年营收与估值是否合理？",
            analysis_as_of=_CUTOFF,
            strategy="分析重点：结构化综合。",
        )
        assert context.research_question
        assert context.analysis_as_of == _CUTOFF
        assert context.strategy


# ---------------------------------------------------------------- schema 局部规则


class TestOutputSchema:
    def test_rejects_blank_summary(self) -> None:
        with pytest.raises(ValidationError):
            _valid_output().__class__(
                summary="   ",
                themes=_valid_output().themes,
                claim_roles=_valid_output().claim_roles,
                duplicates=[],
                conflicts=[],
                evidence_gaps=[],
            )

    def test_rejects_empty_themes(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisAnalysisOutput(
                summary="summary",
                themes=[],
                claim_roles=_roles("C1", "C2"),
                duplicates=[],
                conflicts=[],
                evidence_gaps=[],
            )

    def test_rejects_empty_claim_roles(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisAnalysisOutput(
                summary="summary",
                themes=[SynthesisTheme(title="t", summary="s", claim_refs=["C1"])],
                claim_roles=[],
                duplicates=[],
                conflicts=[],
                evidence_gaps=[],
            )

    def test_rejects_duplicate_claim_refs_in_theme(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisTheme(title="t", summary="s", claim_refs=["C1", "C1"])

    def test_rejects_duplicate_group_smaller_than_two(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisDuplicate(claim_refs=["C1"], canonical_ref="C1", rationale="r")

    def test_rejects_duplicate_canonical_outside_group(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisDuplicate(claim_refs=["C1", "C2"], canonical_ref="C3", rationale="r")

    def test_rejects_duplicate_group_with_duplicate_refs(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisDuplicate(claim_refs=["C1", "C1"], canonical_ref="C1", rationale="r")

    def test_rejects_conflict_group_smaller_than_two(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisConflict(
                claim_refs=["C1"],
                description="冲突",
                severity=SynthesisSeverity.HIGH,
                resolution_direction="方向",
            )

    def test_rejects_blank_conflict_description(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisConflict(
                claim_refs=["C1", "C2"],
                description="  ",
                severity=SynthesisSeverity.HIGH,
                resolution_direction="方向",
            )

    def test_rejects_blank_evidence_gap_description(self) -> None:
        with pytest.raises(ValidationError):
            SynthesisEvidenceGap(
                description=" ",
                claim_refs=["C1"],
                priority=SynthesisPriority.LOW,
            )

    def test_accepts_gap_without_suggested_evidence(self) -> None:
        gap = SynthesisEvidenceGap(
            description="缺口", claim_refs=["C1"], priority=SynthesisPriority.LOW
        )
        assert gap.suggested_evidence is None


# ---------------------------------------------------------------- validate_synthesis_output


class TestValidateSynthesisOutput:
    def test_valid_output_passes(self) -> None:
        output = _valid_output("C1", "C2", "C3", "C4")
        validate_synthesis_output(output, ["C1", "C2", "C3", "C4"])

    def test_unknown_ref_in_theme_rejected(self) -> None:
        output = _valid_output("C1", "C2", "C3")
        output = output.model_copy(
            update={"themes": [SynthesisTheme(title="t", summary="s", claim_refs=["C99"])]}
        )
        with pytest.raises(SynthesisAnalysisUnknownRef):
            validate_synthesis_output(output, ["C1", "C2", "C3"])

    def test_unknown_ref_in_evidence_gap_rejected(self) -> None:
        output = _valid_output("C1", "C2")
        output = output.model_copy(
            update={
                "evidence_gaps": [
                    SynthesisEvidenceGap(
                        description="缺口", claim_refs=["C88"], priority=SynthesisPriority.LOW
                    )
                ]
            }
        )
        with pytest.raises(SynthesisAnalysisUnknownRef):
            validate_synthesis_output(output, ["C1", "C2"])

    def test_missing_role_rejected_no_cherry_picking(self) -> None:
        # input 3 条，roles 只覆盖 C1/C2 → 缺漏 C3。
        output = _valid_output("C1", "C2")
        with pytest.raises(SynthesisAnalysisNoCherryPicking):
            validate_synthesis_output(output, ["C1", "C2", "C3"])

    def test_duplicate_role_rejected_no_cherry_picking(self) -> None:
        # input 2 条，roles 覆盖 C1 两次、C2 一次 → C1 重复。
        output = _valid_output("C1", "C2")
        output = output.model_copy(update={"claim_roles": _roles("C1", "C1", "C2")})
        with pytest.raises(SynthesisAnalysisNoCherryPicking):
            validate_synthesis_output(output, ["C1", "C2"])

    def test_self_invented_role_rejected(self) -> None:
        # roles 里出现 input 不存在的 C99 → UnknownRef（不是 NoCherryPicking）。
        output = _valid_output("C1", "C2")
        output = output.model_copy(update={"claim_roles": _roles("C1", "C2", "C99")})
        with pytest.raises(SynthesisAnalysisUnknownRef):
            validate_synthesis_output(output, ["C1", "C2"])


# ---------------------------------------------------------------- fingerprint


class TestResultFingerprint:
    def _fp(
        self,
        *,
        schema_version: int = SYNTHESIS_RESULT_SCHEMA_VERSION,
        synthesis_fingerprint: str = "a" * 64,
        analyst_name: str = SYNTHESIS_ANALYST_NAME,
        analyst_version: int = SYNTHESIS_ANALYST_VERSION,
        analyst_model_id: str = "deepseek:deepseek-v4-flash",
        output: SynthesisAnalysisOutput | None = None,
    ) -> str:
        return compute_synthesis_result_fingerprint(
            result_schema_version=schema_version,
            synthesis_fingerprint=synthesis_fingerprint,
            analyst_name=analyst_name,
            analyst_version=analyst_version,
            analyst_model_id=analyst_model_id,
            output=output if output is not None else _valid_output("C1", "C2", "C3", "C4"),
        )

    def test_deterministic_same_input(self) -> None:
        assert self._fp() == self._fp()
        assert len(self._fp()) == 64

    def test_changes_with_output(self) -> None:
        base = self._fp()
        altered = _valid_output("C1", "C2", "C3", "C4").model_copy(update={"summary": "不同总结。"})
        assert base != self._fp(output=altered)

    def test_changes_with_analyst_model_id(self) -> None:
        base = self._fp()
        assert base != self._fp(analyst_model_id="deepseek:other-model")

    def test_changes_with_analyst_name(self) -> None:
        base = self._fp()
        assert base != self._fp(analyst_name="other_analyst")

    def test_changes_with_analyst_version(self) -> None:
        base = self._fp()
        assert base != self._fp(analyst_version=2)

    def test_changes_with_synthesis_fingerprint(self) -> None:
        base = self._fp()
        assert base != self._fp(synthesis_fingerprint="b" * 64)

    def test_changes_with_schema_version(self) -> None:
        base = self._fp()
        assert base != self._fp(schema_version=2)

    def test_stable_across_recomputation_no_synthesis_id(self) -> None:
        # 指纹只由确定性输入决定；函数签名本身不接受 synthesis_id / created_at。
        assert self._fp() == self._fp()
