"""Structured claim synthesis contracts (stage 4D.1B): request + LLM output + fingerprint.

角色边界（综合分析只做判断与综合，确定性交给代码）：
- LLM 负责：对一组**已验证**的输入 Claim 做综合——识别主题（themes）、为每条
  Claim 分配综合角色（claim_roles）、检测重复声明（duplicates）与冲突声明
  （conflicts）、指出证据缺口（evidence_gaps）、给出综合总结（summary）；
- 确定性代码负责：C alias（C1..Cn）构造与校验（按 analysis_domain + claim_id
  canonical 排序，**LLM 永不看 UUID**）、ClaimIntegrityGateway 完整校验、
  no-cherry-picking 硬边界（claim_roles 恰好覆盖每条 input Claim）、strict
  validation、result_fingerprint、create_or_get 原子持久化 / replay 校验；
- LLM **不负责**：Retrieval / 访问 Chroma / 读 RawArtifact / 写数据库 / 计算。

冻结常量：
- `SYNTHESIS_RESULT_SCHEMA_VERSION = 1`（claim_synthesis_results.result_schema_version）；
- `SYNTHESIS_ANALYST_NAME = "structured_claim_synthesis_analyst"`；
  `SYNTHESIS_ANALYST_VERSION = 1`（persisted analyst provenance）。
- C ref 格式 `C<number>`，编号来自 ClaimPack（1..n，analysis_domain + claim_id
  排序）；LLM 输出任何非此格式 / 超出范围的编号 → 拒绝。

strict validation（服务层 `validate_synthesis_output`）：
- 全部 C refs 必须是已知 alias（未知 → SynthesisAnalysisUnknownRef）；
- claim_roles **恰好覆盖**每条 input Claim 一次（缺漏 / 重复 → NoCherryPicking）；
- themes 至少 1 个；duplicate/conflict 组 ≥2 个 ref 且 canonical 在组内；
  全部文本字段 trim 非空。
"""

import hashlib
import json
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.analysis.synthesis.errors import (
    SynthesisAnalysisInputError,
    SynthesisAnalysisNoCherryPicking,
    SynthesisAnalysisUnknownRef,
)

# claim_synthesis_results.result_schema_version 的当前值（改名或换结构时递增；
# 已有结果原样保留，新语义 → 新 fingerprint → 新结果行）。
SYNTHESIS_RESULT_SCHEMA_VERSION = 1

# structured claim synthesis analyst 的身份常量（persisted analyst_name）。
SYNTHESIS_ANALYST_NAME = "structured_claim_synthesis_analyst"
SYNTHESIS_ANALYST_VERSION = 1

# 综合分析重点（只做判断与综合；确定性交给代码）。
SYNTHESIS_ANALYST_FOCUS = (
    "分析重点：对一组已验证的输入 Claim 做结构化综合——识别主题（themes）、为每条"
    "Claim 分配综合角色（claim_roles）、检测重复（duplicates）与冲突（conflicts）、"
    "指出证据缺口（evidence_gaps）、给出综合总结（summary）。只依据给定 Claim 综合，"
    "不补充不存在的信息，不做任何计算、不做买入/卖出/目标价/收益预测。"
)

_CLAIM_REF_PATTERN = re.compile(r"^C\d+$")


def _valid_claim_ref(ref: str) -> bool:
    return isinstance(ref, str) and bool(_CLAIM_REF_PATTERN.fullmatch(ref))


def _trim_nonempty(value: str, field: str) -> str:
    trimmed = value.strip()
    if not trimmed:
        raise ValueError(f"{field} 不能为空（trim 后）")
    return trimmed


class SynthesisClaimRole(StrEnum):
    """每条 input Claim 在综合中的角色。

    - support：直接支持研究问题的核心结论；
    - contradict：与研究问题或核心结论矛盾 / 反对；
    - context：提供背景 / 上下文，不直接支持也不反对；
    - qualification：限定 / 条件（依赖特定假设或情景才成立）。
    """

    SUPPORT = "support"
    CONTRADICT = "contradict"
    CONTEXT = "context"
    QUALIFICATION = "qualification"


class SynthesisSeverity(StrEnum):
    """冲突严重度。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SynthesisPriority(StrEnum):
    """证据缺口优先级。"""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SynthesisTheme(BaseModel):
    """一个综合主题：标题 + 摘要 + 关联的 input Claim（C 编号）。"""

    model_config = ConfigDict(frozen=True)

    title: str
    summary: str
    claim_refs: list[str]

    @model_validator(mode="after")
    def _validate(self) -> "SynthesisTheme":
        object.__setattr__(self, "title", _trim_nonempty(self.title, "theme.title"))
        object.__setattr__(self, "summary", _trim_nonempty(self.summary, "theme.summary"))
        if not isinstance(self.claim_refs, list):
            raise ValueError("theme.claim_refs 必须是 list")
        if any(not _valid_claim_ref(ref) for ref in self.claim_refs):
            raise ValueError("theme.claim_refs 必须是 C<number> 格式")
        if len(self.claim_refs) != len(set(self.claim_refs)):
            raise ValueError("theme.claim_refs 不能重复")
        return self


class SynthesisClaimRoleAssignment(BaseModel):
    """一条 input Claim 的综合角色分配（no-cherry-picking 的对象）。"""

    model_config = ConfigDict(frozen=True)

    claim_ref: str
    role: SynthesisClaimRole
    rationale: str

    @model_validator(mode="after")
    def _validate(self) -> "SynthesisClaimRoleAssignment":
        if not _valid_claim_ref(self.claim_ref):
            raise ValueError("claim_roles.claim_ref 必须是 C<number> 格式")
        object.__setattr__(
            self, "rationale", _trim_nonempty(self.rationale, "claim_roles.rationale")
        )
        return self


class SynthesisDuplicate(BaseModel):
    """一组重复 / 近似重复声明：合并后保留 canonical 一条。"""

    model_config = ConfigDict(frozen=True)

    claim_refs: list[str]
    canonical_ref: str
    rationale: str

    @model_validator(mode="after")
    def _validate(self) -> "SynthesisDuplicate":
        if not isinstance(self.claim_refs, list) or len(self.claim_refs) < 2:
            raise ValueError("duplicate 至少需要 2 个 claim_ref")
        if any(not _valid_claim_ref(ref) for ref in self.claim_refs):
            raise ValueError("duplicates.claim_refs 必须是 C<number> 格式")
        if len(self.claim_refs) != len(set(self.claim_refs)):
            raise ValueError("duplicates.claim_refs 不能重复")
        if self.canonical_ref not in self.claim_refs:
            raise ValueError("duplicates.canonical_ref 必须是组内 claim 之一")
        object.__setattr__(
            self, "rationale", _trim_nonempty(self.rationale, "duplicates.rationale")
        )
        return self


class SynthesisConflict(BaseModel):
    """一组冲突声明 + 冲突描述 / 严重度 / 解决方向。"""

    model_config = ConfigDict(frozen=True)

    claim_refs: list[str]
    description: str
    severity: SynthesisSeverity
    resolution_direction: str

    @model_validator(mode="after")
    def _validate(self) -> "SynthesisConflict":
        if not isinstance(self.claim_refs, list) or len(self.claim_refs) < 2:
            raise ValueError("conflict 至少需要 2 个 claim_ref")
        if any(not _valid_claim_ref(ref) for ref in self.claim_refs):
            raise ValueError("conflicts.claim_refs 必须是 C<number> 格式")
        if len(self.claim_refs) != len(set(self.claim_refs)):
            raise ValueError("conflicts.claim_refs 不能重复")
        object.__setattr__(
            self, "description", _trim_nonempty(self.description, "conflicts.description")
        )
        object.__setattr__(
            self,
            "resolution_direction",
            _trim_nonempty(self.resolution_direction, "conflicts.resolution_direction"),
        )
        return self


class SynthesisEvidenceGap(BaseModel):
    """一个证据缺口：缺什么证据、关联哪些 Claim、建议补什么、优先级。"""

    model_config = ConfigDict(frozen=True)

    description: str
    claim_refs: list[str]
    suggested_evidence: str | None = None
    priority: SynthesisPriority

    @model_validator(mode="after")
    def _validate(self) -> "SynthesisEvidenceGap":
        object.__setattr__(
            self, "description", _trim_nonempty(self.description, "evidence_gaps.description")
        )
        if not isinstance(self.claim_refs, list):
            raise ValueError("evidence_gaps.claim_refs 必须是 list")
        if any(not _valid_claim_ref(ref) for ref in self.claim_refs):
            raise ValueError("evidence_gaps.claim_refs 必须是 C<number> 格式")
        if self.suggested_evidence is not None:
            object.__setattr__(
                self,
                "suggested_evidence",
                _trim_nonempty(self.suggested_evidence, "evidence_gaps.suggested_evidence"),
            )
        return self


class SynthesisAnalysisOutput(BaseModel):
    """一次综合的结构化输出（Pydantic 结构化输出，模型生成）。

    schema 层只强制文本非空 / ref 格式 / 组大小等局部规则；**no-cherry-picking
    与 unknown-ref 需要在服务层（input claim set 已知）校验**。
    """

    model_config = ConfigDict(frozen=True)

    summary: str
    themes: list[SynthesisTheme]
    claim_roles: list[SynthesisClaimRoleAssignment]
    duplicates: list[SynthesisDuplicate]
    conflicts: list[SynthesisConflict]
    evidence_gaps: list[SynthesisEvidenceGap]

    @model_validator(mode="after")
    def _validate(self) -> "SynthesisAnalysisOutput":
        object.__setattr__(self, "summary", _trim_nonempty(self.summary, "summary"))
        if not isinstance(self.themes, list) or not self.themes:
            raise ValueError("themes 至少需要 1 个")
        if not isinstance(self.claim_roles, list) or not self.claim_roles:
            raise ValueError("claim_roles 不能为空（no-cherry-picking 由服务层校验）")
        return self


@dataclass(frozen=True)
class SynthesisAnalysisRequest:
    """调用方提交的综合分析请求。

    **只提供 synthesis_id**：research_question / cutoff / company / Claim 输入集
    一律从既有 SynthesisRun + gateway 校验的真实 Claims 派生，调用方不得提供。
    """

    synthesis_id: UUID

    def __post_init__(self) -> None:
        if isinstance(self.synthesis_id, bool) or not isinstance(self.synthesis_id, UUID):
            raise SynthesisAnalysisInputError("synthesis_id 必须是 UUID")


@dataclass(frozen=True)
class SynthesisAnalysisContext:
    """传给模型的本次分析元数据（analysis_domain 固定为 claim_synthesis，不是 LLM 决定）。"""

    research_question: str
    analysis_as_of: date
    strategy: str


@dataclass(frozen=True)
class SynthesisAnalysisResult:
    """一次综合分析的结果摘要（不含 themes / conflicts / 任何正文文本）。"""

    synthesis_result_id: UUID
    synthesis_id: UUID
    result_fingerprint: str
    replayed: bool
    claim_count: int


@dataclass(frozen=True)
class VerifiedSynthesisResult:
    """经 `verify_result_integrity` 完整校验后的 SynthesisResult 投影（不可变）。

    Stage 5A：ReportOutlineService 的 **verified immutable input**——消费方只
    消费本投影派生提纲，**不复制** SynthesisRun replay 规则 / 不重复实现
    integrity 校验。校验覆盖：run 完整 + result schema + analyst 身份 + payload
    可解析 + resolved claim IDs 全属 exact input set + 重算 result_fingerprint
    一致；任一损坏 → `SynthesisResultIntegrityError`（**不自动 repair**）。

    - input_claim_ids：exact input set（canonical 排序，与 fingerprint 一致）；
    - alias_map：C alias → 真实 claim_id（LLM 输出的 C 编号解析回 UUID）；
    - output：从 persisted JSONB 重新解析并完整校验的 `SynthesisAnalysisOutput`。
    """

    synthesis_result_id: UUID
    synthesis_id: UUID
    company_id: UUID
    research_question: str
    research_question_sha256: str
    analysis_as_of: date
    synthesis_fingerprint: str
    result_fingerprint: str
    input_claim_ids: tuple[UUID, ...]
    alias_map: dict[str, UUID]
    output: SynthesisAnalysisOutput


def validate_synthesis_output(
    output: SynthesisAnalysisOutput,
    claim_refs: list[str],
) -> None:
    """strict validation（服务层）：全部 C refs 已知 + no-cherry-picking 硬边界。

    - 每条 input claim 在 claim_roles 中**恰好出现一次**（缺漏 / 重复 →
      NoCherryPicking，不静默补齐）；
    - 所有输出 section 引用的 C refs 必须是已知 alias（未知 → UnknownRef）。
    """
    known = set(claim_refs)

    def _check_known(refs: list[str], where: str) -> None:
        for ref in refs:
            if ref not in known:
                raise SynthesisAnalysisUnknownRef(f"{where} 引用了未知 C ref: {ref}")

    for theme in output.themes:
        _check_known(theme.claim_refs, f"theme[{theme.title}]")
    for gap in output.evidence_gaps:
        _check_known(gap.claim_refs, "evidence_gaps")

    roles = [assignment.claim_ref for assignment in output.claim_roles]
    counts = Counter(roles)
    for ref in claim_refs:
        if counts[ref] != 1:
            raise SynthesisAnalysisNoCherryPicking(
                f"claim_roles 必须恰好覆盖每条 input claim "
                f"（{ref} 出现 {counts[ref]} 次，期望 1 次）"
            )
    for ref in counts:
        if ref not in known:
            raise SynthesisAnalysisUnknownRef(f"claim_roles 引用了未知 C ref: {ref}")

    for duplicate in output.duplicates:
        _check_known(duplicate.claim_refs, "duplicates")
    for conflict in output.conflicts:
        _check_known(conflict.claim_refs, "conflicts")


def compute_synthesis_result_fingerprint(
    *,
    result_schema_version: int,
    synthesis_fingerprint: str,
    analyst_name: str,
    analyst_version: int,
    analyst_model_id: str,
    output: SynthesisAnalysisOutput,
) -> str:
    """确定性 SHA-256 指纹（sort_keys + 固定 separators + UTF-8）。

    至少覆盖：result_schema_version、synthesis_fingerprint（= run 的完整输入
    边界）、analyst_name / version / model_id、output 的全部结构化内容
    （canonical JSON）。

    **不得包含** synthesis_result_id / synthesis_id / created_at。同 run + 同
    analyst + 同输出 → 同指纹 → replay 同一结果；run 输入 / analyst 版本 / 输出
    任一变化 → 新指纹 → 新结果（旧行保留，无 update API）。
    """
    payload = {
        "result_schema_version": result_schema_version,
        "synthesis_fingerprint": synthesis_fingerprint,
        "analyst_name": analyst_name,
        "analyst_version": analyst_version,
        "analyst_model_id": analyst_model_id,
        "output": output.model_dump(mode="json"),
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()
