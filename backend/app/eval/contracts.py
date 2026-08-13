"""Evaluation data contracts (stage 7B.1.0).

冻结三路系统评估的数据契约：frozen source snapshot（document / macro /
structured）、`EvalCase`、`EvalDatasetManifest`、typed `HumanLabel`、variant
execution config、execution/scoring 分离的 spec、normalized variant output。

关键冻结语义：
- Source Snapshot 是**原始 source 的字节寻址 manifest**（content_sha256 /
  snapshot_fingerprint / artifact_fingerprint），**不保存 raw bytes**，也**不**把
  EvidenceCard/Claim 当 baseline 默认输入；三条 pipeline 各自从同一 snapshot 做
  自己的提取。
- HumanLabel 是 typed（4 个 discriminated label），**不**是 generic (type, value)
  LabelEntry；free-text `annotation` 只做人工备注，**不**进入 machine
  ground-truth（fingerprint 排除 annotation）。
- Execution 与 Scoring 分离：`EvalExecutionSpec` 只表达「系统实际看到什么 + 以什么
  配置运行」，**不含** human_label_fingerprint / metric_registry_version / judge；
  `EvalScoringSpec` 才绑定 label / metric registry / judge config。
- `EvalVariantOutput` 是三种 variant 最终都必须产出的 normalized structure；
  baseline 不得用后置强 LLM 把纯文本解析成该结构（公平性边界）。

所有 unordered collection → fingerprint 层 canonical sort；本层 validator 显式拒绝
duplicate identity。
"""

import re
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.eval.metrics import METRIC_REGISTRY_VERSION
from app.eval.variants import EvalVariantId

# ---------------------------------------------------------------- schema 版本

SNAPSHOT_SCHEMA_VERSION = 1
EVAL_CASE_SCHEMA_VERSION = 1
EVAL_DATASET_SCHEMA_VERSION = 1
HUMAN_LABEL_SCHEMA_VERSION = 1
EVAL_EXECUTION_CONFIG_SCHEMA_VERSION = 1
EVAL_EXECUTION_SPEC_SCHEMA_VERSION = 1
EVAL_SCORING_SPEC_SCHEMA_VERSION = 1
EVAL_VARIANT_OUTPUT_SCHEMA_VERSION = 1

# ---------------------------------------------------------------- 校验 helpers

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_HEX_DIGITS = frozenset("0123456789abcdef")

_MAX_SLUG_LENGTH = 128
_MAX_QUESTION_LENGTH = 4000
_MAX_SECURITY_CODE_LENGTH = 32
_MAX_TAG_LENGTH = 64
_MAX_COMPONENT_NAME_LENGTH = 128
_MAX_COMPONENT_VERSION_LENGTH = 64


def _is_sha256_hex(value: str) -> bool:
    return len(value) == 64 and all(c in _HEX_DIGITS for c in value)


def _strip(value: str, *, field: str, max_len: int | None = None) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空（trim 后）")
    if max_len is not None and len(value) > max_len:
        raise ValueError(f"{field} 长度超限（>{max_len}）")
    return value


def _validate_sha256(value: str, *, field: str) -> str:
    if not _is_sha256_hex(value):
        raise ValueError(f"{field} 必须是 64 位小写 hex")
    return value


def _reject_uuid_slug(value: str) -> str:
    if _UUID_RE.fullmatch(value):
        raise ValueError("不允许 UUID-only identity 作为 slug")
    return value


def _validate_slug(value: str) -> str:
    return _reject_uuid_slug(_strip(value, field="slug", max_len=_MAX_SLUG_LENGTH))


# ---------------------------------------------------------------- source snapshot


class FrozenDocumentSourceRef(BaseModel):
    """一条 document source 的字节寻址引用（不含 raw bytes）。

    UUID（source_record_id / raw_artifact_id）用于执行期加载；`content_sha256` 是
    semantic content identity（进入 fingerprint，UUID 不进入）。
    """

    model_config = ConfigDict(frozen=True)

    source_record_id: UUID
    raw_artifact_id: UUID
    content_sha256: str
    provider_key: str
    document_type: str
    media_type: str
    published_at: datetime | None = None
    reporting_period_start: date | None = None
    reporting_period_end: date | None = None

    @field_validator("content_sha256")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="content_sha256")

    @field_validator("provider_key", "document_type", "media_type")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="source 元数据")


class FrozenMacroSnapshotRef(BaseModel):
    """一条 macro snapshot 的字节寻址引用（`snapshot_fingerprint` 是 semantic identity）。

    `payload_sha256` 是 Evaluation Bundle 冻结的 canonical payload bytes identity——
    独立证明 payload bytes 未被篡改，但**不**参与 duplicate identity（仍按
    `snapshot_fingerprint` 去重）。
    """

    model_config = ConfigDict(frozen=True)

    snapshot_id: UUID
    series_id: UUID
    snapshot_fingerprint: str
    payload_sha256: str
    fetched_at: datetime

    @field_validator("snapshot_fingerprint")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="snapshot_fingerprint")

    @field_validator("payload_sha256")
    @classmethod
    def _v_payload_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="payload_sha256")


class StructuredArtifactType(StrEnum):
    FINANCIAL_METRIC_OBSERVATION = "financial_metric_observation"
    RELATIVE_VALUATION_OBSERVATION = "relative_valuation_observation"
    RELATIVE_VALUATION_COMPARISON = "relative_valuation_comparison"


class FrozenStructuredArtifactRef(BaseModel):
    """一条 structured artifact 的字节寻址引用（`artifact_fingerprint` 是 semantic identity）。

    `payload_sha256` 是 Evaluation Bundle 冻结的 canonical payload bytes identity——
    独立证明 payload bytes 未被篡改，但**不**参与 duplicate identity（仍按
    `(artifact_type, artifact_fingerprint)` 去重）。
    """

    model_config = ConfigDict(frozen=True)

    artifact_type: StructuredArtifactType
    artifact_id: UUID
    artifact_fingerprint: str
    payload_sha256: str

    @field_validator("artifact_fingerprint")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="artifact_fingerprint")

    @field_validator("payload_sha256")
    @classmethod
    def _v_payload_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="payload_sha256")


class FrozenSourceSnapshot(BaseModel):
    """三路评估的 frozen source snapshot manifest（document / macro / structured）。

    collection 语义 unordered，duplicate identity 构造时拒绝；不含 raw bytes。
    """

    model_config = ConfigDict(frozen=True)

    snapshot_schema_version: int = SNAPSHOT_SCHEMA_VERSION
    document_sources: tuple[FrozenDocumentSourceRef, ...] = ()
    macro_snapshots: tuple[FrozenMacroSnapshotRef, ...] = ()
    structured_artifacts: tuple[FrozenStructuredArtifactRef, ...] = ()

    @model_validator(mode="after")
    def _reject_duplicate_identity(self) -> "FrozenSourceSnapshot":
        # duplicate semantic identity：UUID 只做 provenance pointer，不决定
        # 「是否同一 frozen input」。document 用 content_sha256，macro 用
        # snapshot_fingerprint，structured 用 (artifact_type, artifact_fingerprint)。
        seen_doc: set[str] = set()
        for ref in self.document_sources:
            if ref.content_sha256 in seen_doc:
                raise ValueError("duplicate document source identity (content_sha256)")
            seen_doc.add(ref.content_sha256)
        seen_macro: set[str] = set()
        for ref in self.macro_snapshots:
            if ref.snapshot_fingerprint in seen_macro:
                raise ValueError("duplicate macro snapshot identity (snapshot_fingerprint)")
            seen_macro.add(ref.snapshot_fingerprint)
        seen_art: set[tuple[StructuredArtifactType, str]] = set()
        for ref in self.structured_artifacts:
            key = (ref.artifact_type, ref.artifact_fingerprint)
            if key in seen_art:
                raise ValueError(
                    "duplicate structured artifact identity (artifact_type, artifact_fingerprint)"
                )
            seen_art.add(key)
        return self


# ---------------------------------------------------------------- eval case


class EvalCase(BaseModel):
    """单个研究 case 的语义定义（不含 execution status / runtime id）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = EVAL_CASE_SCHEMA_VERSION
    case_id: str
    case_version: int = Field(ge=1)
    company_id: UUID
    security_code: str
    research_question: str
    analysis_as_of: datetime
    tags: tuple[str, ...] = ()
    source_snapshot_fingerprint: str
    human_label_fingerprint: str | None = None

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("security_code")
    @classmethod
    def _v_security_code(cls, v: str) -> str:
        return _strip(v, field="security_code", max_len=_MAX_SECURITY_CODE_LENGTH)

    @field_validator("research_question")
    @classmethod
    def _v_question(cls, v: str) -> str:
        return _strip(v, field="research_question", max_len=_MAX_QUESTION_LENGTH)

    @field_validator("tags")
    @classmethod
    def _v_tags(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip(t, field="tag", max_len=_MAX_TAG_LENGTH) for t in v)

    @field_validator("source_snapshot_fingerprint")
    @classmethod
    def _v_snapshot_fp(cls, v: str) -> str:
        return _validate_sha256(v, field="source_snapshot_fingerprint")

    @field_validator("human_label_fingerprint")
    @classmethod
    def _v_label_fp(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_sha256(v, field="human_label_fingerprint")
        return v


# ---------------------------------------------------------------- dataset manifest


class EvalDatasetCaseRef(BaseModel):
    """dataset 里对某个 case 版本的引用。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_version: int = Field(ge=1)
    case_fingerprint: str

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("case_fingerprint")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="case_fingerprint")


class EvalDatasetManifest(BaseModel):
    """frozen eval dataset 的 manifest（dataset 语义身份 + 有序 canonical case refs）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = EVAL_DATASET_SCHEMA_VERSION
    dataset_id: str
    dataset_version: int = Field(ge=1)
    cases: tuple[EvalDatasetCaseRef, ...] = ()
    description: str | None = None

    @field_validator("dataset_id")
    @classmethod
    def _v_dataset_id(cls, v: str) -> str:
        return _validate_slug(v)

    @model_validator(mode="after")
    def _reject_duplicate_cases(self) -> "EvalDatasetManifest":
        seen: set[tuple[str, int]] = set()
        for ref in self.cases:
            key = (ref.case_id, ref.case_version)
            if key in seen:
                raise ValueError(f"duplicate dataset case: {ref.case_id} v{ref.case_version}")
            seen.add(key)
        return self


# ---------------------------------------------------------------- human labels


class ClaimSupportStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"
    CONFLICTED = "conflicted"


class FinancialFactLabel(BaseModel):
    """financial_number_accuracy 的人工 ground-truth（typed，非 free-text）。"""

    model_config = ConfigDict(frozen=True)

    label_type: Literal["financial_fact"] = "financial_fact"
    metric_code: str
    period: str
    scope: str | None = None
    unit: str
    expected_value: Decimal
    absolute_tolerance: Decimal = Decimal("0")
    relative_tolerance: Decimal = Decimal("0")

    @field_validator("metric_code", "period", "unit")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="label 文本字段")

    @field_validator("scope")
    @classmethod
    def _v_scope(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("absolute_tolerance", "relative_tolerance")
    @classmethod
    def _v_tolerance(cls, v: Decimal) -> Decimal:
        if v < 0:
            raise ValueError("tolerance 必须 >= 0")
        return v


class RiskTopicLabel(BaseModel):
    """risk_topic_recall 的人工 ground-truth。"""

    model_config = ConfigDict(frozen=True)

    label_type: Literal["risk_topic"] = "risk_topic"
    risk_code: str
    required: bool
    acceptable_aliases: tuple[str, ...] = ()

    @field_validator("risk_code")
    @classmethod
    def _v_risk_code(cls, v: str) -> str:
        return _strip(v, field="risk_code")

    @field_validator("acceptable_aliases")
    @classmethod
    def _v_aliases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip(a, field="alias") for a in v)


class ClaimSupportLabel(BaseModel):
    """claim_support 的人工 ground-truth。"""

    model_config = ConfigDict(frozen=True)

    label_type: Literal["claim_support"] = "claim_support"
    claim_label_id: str
    expected_support_status: ClaimSupportStatus
    related_source_fingerprints: tuple[str, ...] = ()

    @field_validator("claim_label_id")
    @classmethod
    def _v_claim_label_id(cls, v: str) -> str:
        return _strip(v, field="claim_label_id")

    @field_validator("related_source_fingerprints")
    @classmethod
    def _v_related_fps(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_validate_sha256(fp, field="related_source_fingerprints") for fp in v)


class MacroCausalLabel(BaseModel):
    """macro_causal_error 的人工 ground-truth。"""

    model_config = ConfigDict(frozen=True)

    label_type: Literal["macro_causal"] = "macro_causal"
    driver_code: str
    company_exposure_expected: bool
    causal_claim_allowed: bool

    @field_validator("driver_code")
    @classmethod
    def _v_driver_code(cls, v: str) -> str:
        return _strip(v, field="driver_code")


class HumanLabel(BaseModel):
    """一个 case 的结构化人工标注（typed；annotation 不参与 machine ground-truth）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = HUMAN_LABEL_SCHEMA_VERSION
    case_id: str
    case_version: int = Field(ge=1)
    label_version: int = Field(ge=1)
    financial_facts: tuple[FinancialFactLabel, ...] = ()
    risk_topics: tuple[RiskTopicLabel, ...] = ()
    claim_support_labels: tuple[ClaimSupportLabel, ...] = ()
    macro_causal_labels: tuple[MacroCausalLabel, ...] = ()
    annotation: str | None = None

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _validate_slug(v)


# ---------------------------------------------------------------- execution config


class FrozenModelConfig(BaseModel):
    """逐 variant 冻结的模型 / 参数（不含 API key）。"""

    model_config = ConfigDict(frozen=True)

    provider: str
    model_id: str
    thinking_enabled: bool
    temperature: Decimal
    max_output_tokens: int | None = None
    structured_output: bool

    @field_validator("provider", "model_id")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="model 字段")

    @field_validator("temperature")
    @classmethod
    def _v_temperature(cls, v: Decimal) -> Decimal:
        if v < 0 or v > 2:
            raise ValueError("temperature 必须在 [0, 2]")
        return v

    @field_validator("max_output_tokens")
    @classmethod
    def _v_max_tokens(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("max_output_tokens 必须 >= 1")
        return v


class EvalComponentVersion(BaseModel):
    """单个 pipeline component 的冻结版本（`component_name` → `component_version`）。

    用于精确冻结 Full pipeline 多个 component 的真实版本（如 evidence_extractor:v2、
    audit:v1），弥补仅有 variant_version/prompt_version/retrieval_version/
    pipeline_version 的粒度不足。
    """

    model_config = ConfigDict(frozen=True)

    component_name: str
    component_version: str

    @field_validator("component_name")
    @classmethod
    def _v_name(cls, v: str) -> str:
        return _strip(v, field="component_name", max_len=_MAX_COMPONENT_NAME_LENGTH)

    @field_validator("component_version")
    @classmethod
    def _v_version(cls, v: str) -> str:
        return _strip(v, field="component_version", max_len=_MAX_COMPONENT_VERSION_LENGTH)


class EvalExecutionConfig(BaseModel):
    """variant 执行配置（bounded deterministic settings + frozen model + component versions）。"""

    model_config = ConfigDict(frozen=True)

    config_schema_version: int = EVAL_EXECUTION_CONFIG_SCHEMA_VERSION
    variant_id: EvalVariantId
    model: FrozenModelConfig
    variant_version: str
    prompt_version: str
    retrieval_version: str
    pipeline_version: str
    retrieval_top_k: int | None = None
    component_versions: tuple[EvalComponentVersion, ...] = ()

    @field_validator("variant_version", "prompt_version", "retrieval_version", "pipeline_version")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="version 字段")

    @field_validator("retrieval_top_k")
    @classmethod
    def _v_top_k(cls, v: int | None) -> int | None:
        if v is not None and v < 1:
            raise ValueError("retrieval_top_k 必须 >= 1")
        return v

    @field_validator("component_versions")
    @classmethod
    def _v_component_versions(
        cls, v: tuple[EvalComponentVersion, ...]
    ) -> tuple[EvalComponentVersion, ...]:
        # component_name 唯一 + canonical 排序为 sorted tuple（让 tuple 输入顺序
        # 不影响指纹；fingerprint 层仍再 canonical sort 一次以防御）。
        seen: set[str] = set()
        for cv in v:
            if cv.component_name in seen:
                raise ValueError(f"duplicate component_name: {cv.component_name}")
            seen.add(cv.component_name)
        return tuple(sorted(v, key=lambda c: c.component_name))


# ---------------------------------------------------------------- execution / scoring spec


class EvalExecutionSpec(BaseModel):
    """「系统实际看到什么 + 以什么配置运行」（不含 label / metric registry / judge）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = EVAL_EXECUTION_SPEC_SCHEMA_VERSION
    case_fingerprint: str
    source_snapshot_fingerprint: str
    execution_config_fingerprint: str
    variant_id: EvalVariantId

    @field_validator(
        "case_fingerprint", "source_snapshot_fingerprint", "execution_config_fingerprint"
    )
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="spec fingerprint")


class EvalScoringSpec(BaseModel):
    """评分侧：绑定 execution 产出 + human label + metric registry + judge config。

    `human_label_fingerprint` 与 `judge_config_fingerprint` 均为可选（None canonical）：
    deterministic scoring spec（只跑 citation_validity / citation_coverage）无需 label
    或 judge；label 缺失不改变 spec 有效性，None 在 fingerprint 中规范为 `null`。
    """

    model_config = ConfigDict(frozen=True)

    schema_version: int = EVAL_SCORING_SPEC_SCHEMA_VERSION
    execution_result_fingerprint: str
    human_label_fingerprint: str | None = None
    metric_registry_version: int = METRIC_REGISTRY_VERSION
    judge_config_fingerprint: str | None = None

    @field_validator("execution_result_fingerprint")
    @classmethod
    def _v_sha_required(cls, v: str) -> str:
        return _validate_sha256(v, field="scoring fingerprint")

    @field_validator("human_label_fingerprint", "judge_config_fingerprint")
    @classmethod
    def _v_sha_optional(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_sha256(v, field="scoring fingerprint")
        return v

    @field_validator("metric_registry_version")
    @classmethod
    def _v_registry_version(cls, v: int) -> int:
        if v < 1:
            raise ValueError("metric_registry_version 必须 >= 1")
        return v


# ---------------------------------------------------------------- normalized output


class EvalCitation(BaseModel):
    """normalized output 的一条 citation。"""

    model_config = ConfigDict(frozen=True)

    citation_id: str
    source_fingerprint: str
    locator: str | None = None
    claim_ids: tuple[str, ...] = ()

    @field_validator("citation_id")
    @classmethod
    def _v_citation_id(cls, v: str) -> str:
        return _strip(v, field="citation_id")

    @field_validator("source_fingerprint")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="source_fingerprint")

    @field_validator("claim_ids")
    @classmethod
    def _v_claim_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip(c, field="claim_id") for c in v)


class EvalClaim(BaseModel):
    """normalized output 的一条 claim。"""

    model_config = ConfigDict(frozen=True)

    claim_id: str
    statement: str
    claim_type: str
    citation_ids: tuple[str, ...] = ()

    @field_validator("claim_id", "claim_type")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="claim 字段")

    @field_validator("statement")
    @classmethod
    def _v_statement(cls, v: str) -> str:
        return _strip(v, field="statement")

    @field_validator("citation_ids")
    @classmethod
    def _v_citation_ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip(c, field="citation_id") for c in v)


class EvalVariantOutput(BaseModel):
    """三种 variant 最终都必须产出的 normalized structure（无 CoT / reasoning / key）。"""

    model_config = ConfigDict(frozen=True)

    schema_version: int = EVAL_VARIANT_OUTPUT_SCHEMA_VERSION
    variant_id: EvalVariantId
    case_id: str
    case_version: int = Field(ge=1)
    final_text: str
    claims: tuple[EvalClaim, ...] = ()
    citations: tuple[EvalCitation, ...] = ()
    report_artifact_ref: str | None = None

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("final_text")
    @classmethod
    def _v_final_text(cls, v: str) -> str:
        return _strip(v, field="final_text")

    @field_validator("report_artifact_ref")
    @classmethod
    def _v_report_ref(cls, v: str | None) -> str | None:
        if v is not None:
            return _strip(v, field="report_artifact_ref")
        return v
