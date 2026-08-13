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

from app.domain.macro_persistence import MacroSnapshotArtifactRole, MacroSnapshotStatus
from app.eval.metrics import METRIC_REGISTRY_VERSION
from app.eval.variants import EvalVariantId

# ---------------------------------------------------------------- schema 版本

SNAPSHOT_SCHEMA_VERSION = 3
EVAL_CASE_SCHEMA_VERSION = 2
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


_MACRO_ARTIFACT_ROLES = frozenset(role.value for role in MacroSnapshotArtifactRole)
_MACRO_METADATA_ROLES = frozenset(
    {
        MacroSnapshotArtifactRole.INDICATOR_METADATA.value,
        MacroSnapshotArtifactRole.COUNTRY_METADATA.value,
    }
)
_MACRO_SNAPSHOT_STATUSES = frozenset(status.value for status in MacroSnapshotStatus)


class StrictFrozenEvalModel(BaseModel):
    """bundle-facing frozen 契约的基类：`frozen=True` + `extra="forbid"`。

    `extra="forbid"` 让 unknown field 在解析时抛 `ValidationError`，而不是被
    Pydantic 静默忽略——防止 schema evolution（字段改名/删除）后旧字段被悄悄
    吞掉，导致 frozen bundle 与当前契约悄悄漂移。所有真正 frozen 的 public
    契约都继承本类。
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- source snapshot


class FrozenDocumentSourceRef(StrictFrozenEvalModel):
    """一条 document source 的字节寻址引用（不含 raw bytes）。

    UUID（source_record_id / raw_artifact_id）用于执行期加载；`content_sha256` 是
    semantic content identity（进入 fingerprint，UUID 不进入）。
    """

    source_record_id: UUID
    raw_artifact_id: UUID
    content_sha256: str
    provider_key: str
    document_type: str
    media_type: str
    title: str
    source_url: str
    acquired_at: datetime
    authority_tier_snapshot: int
    critical_claim_eligible_snapshot: bool
    published_at: datetime | None = None
    reporting_period_start: date | None = None
    reporting_period_end: date | None = None

    @field_validator("content_sha256")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="content_sha256")

    @field_validator("provider_key", "document_type", "media_type", "title", "source_url")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="source 元数据")

    @field_validator("authority_tier_snapshot")
    @classmethod
    def _v_tier(cls, v: int) -> int:
        if v < 1 or v > 4:
            raise ValueError("authority_tier_snapshot 必须在 1..4")
        return v


class FrozenMacroSeriesRef(StrictFrozenEvalModel):
    """MacroSeries 的稳定身份六字段（`series_id` 由父级 `FrozenMacroSnapshotRef` 持有）。

    rehydration 用这六字段精确重建 `macro_series` 行；`created_at` 由 target DB
    server default 生成，不冻结。
    """

    provider_key: str
    source_id: str
    external_indicator_id: str
    geography_type: str
    geography_code: str
    frequency: str

    @field_validator(
        "provider_key",
        "source_id",
        "external_indicator_id",
        "geography_type",
        "geography_code",
        "frequency",
    )
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="macro series 身份字段")


class FrozenMacroTopicRef(StrictFrozenEvalModel):
    """macro indicator 的一个 topic（`topic_id` + `name`）。"""

    topic_id: str
    name: str

    @field_validator("topic_id", "name")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="macro topic")


class FrozenMacroObservationRef(StrictFrozenEvalModel):
    """一条 `MacroObservation` 的冻结行（`observation_id` 精确复现）。

    `snapshot_id` 由父级持有；`period_semantics` / `frequency` 是 frozen-exact
    （materializer 从真实 PG 行投影，replayer 用冻结值精确复现，不覆盖为当前
    模块常量）。`value_numeric` 用 Decimal 保存（JSON 序列化为 str，round-trip
    保留 scale）。
    """

    observation_id: UUID
    period: str
    normalized_period_start: date
    value_numeric: Decimal | None = None
    is_missing: bool
    decimal_scale: int | None = None
    observation_status: str | None = None
    period_semantics: str
    frequency: str

    @field_validator("period")
    @classmethod
    def _v_period(cls, v: str) -> str:
        if not re.fullmatch(r"^\d{4}$", v):
            raise ValueError("period 必须为四位年份")
        return v

    @field_validator("period_semantics", "frequency")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="macro observation 字段")

    @model_validator(mode="after")
    def _v_value_consistency(self) -> "FrozenMacroObservationRef":
        if self.is_missing:
            if self.value_numeric is not None:
                raise ValueError("is_missing=True 时 value_numeric 必须为 None")
            if self.decimal_scale is not None:
                raise ValueError("is_missing=True 时 decimal_scale 必须为 None")
        else:
            if self.value_numeric is None:
                raise ValueError("is_missing=False 时 value_numeric 不能为 None")
            if self.decimal_scale is None or self.decimal_scale < 0:
                raise ValueError("is_missing=False 时 decimal_scale 必须 >= 0")
        return self


class FrozenMacroRawArtifactRef(StrictFrozenEvalModel):
    """一条 macro 原始响应的字节寻址引用（content-addressed blob in
    `blobs/sha256/<first2>/<fullsha>`，与 document blob 共用同一 content-addressed
    布局——SHA 相同则复用同一 blob）。

    `role` / `page` **只**属于 `FrozenMacroArtifactLinkRef`（link 语义），不在此
    重复；raw artifact 是纯字节对象，与 link 是多对一关系（同一 raw 可被多个
    link 引用）。不把 raw bytes base64 进 macro JSON。
    """

    artifact_id: UUID
    content_sha256: str
    media_type: str
    byte_size: int

    @field_validator("content_sha256")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="content_sha256")

    @field_validator("media_type")
    @classmethod
    def _v_media_type(cls, v: str) -> str:
        if v != "application/json":
            raise ValueError("macro raw artifact media_type 必须为 application/json")
        return v

    @field_validator("byte_size")
    @classmethod
    def _v_byte_size(cls, v: int) -> int:
        if v < 1:
            raise ValueError("byte_size 必须 >= 1")
        return v


class FrozenMacroArtifactLinkRef(StrictFrozenEvalModel):
    """一条 `MacroSnapshotArtifactLink` 的冻结行（`snapshot_artifact_id` 精确复现）。

    `snapshot_id` 由父级持有。role 与 page 满足 DB 语义：metadata role 无 page，
    observations_page 必带 page。
    """

    snapshot_artifact_id: UUID
    artifact_id: UUID
    role: str
    page: int | None = None
    response_status: int
    final_hostname: str
    content_type: str
    fetched_at: datetime

    @field_validator("role")
    @classmethod
    def _v_role(cls, v: str) -> str:
        if v not in _MACRO_ARTIFACT_ROLES:
            raise ValueError(f"role 必须为 {sorted(_MACRO_ARTIFACT_ROLES)}")
        return v

    @field_validator("final_hostname", "content_type")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="macro artifact link 字段")

    @model_validator(mode="after")
    def _v_role_page(self) -> "FrozenMacroArtifactLinkRef":
        if self.role in _MACRO_METADATA_ROLES and self.page is not None:
            raise ValueError(f"role={self.role} 时 page 必须为 None")
        if self.role == MacroSnapshotArtifactRole.OBSERVATIONS_PAGE.value:
            if self.page is None or self.page < 1:
                raise ValueError("observations_page 必须带 page >= 1")
        return self


class FrozenMacroSnapshotDetail(StrictFrozenEvalModel):
    """`MacroDatasetSnapshot` 行级语义字段（不含 snapshot_id / series_id /
    snapshot_fingerprint / fetched_at——由父级持有）。

    `fingerprint_version` / `normalization_version` / `status` 是 frozen-exact：
    materializer 从真实 PG 行投影，replayer 用冻结值精确复现（不覆盖为当前
    模块常量 `MACRO_SNAPSHOT_FINGERPRINT_VERSION` / `WORLD_BANK_NORMALIZATION_VERSION`
    / `MacroSnapshotStatus.AVAILABLE`）。

    materializer 从真实 PG 行投影；rehydrator 用它精确重建 snapshot 行。
    """

    requested_country_code: str
    query_start_year: int
    query_end_year: int
    source_id_snapshot: str
    indicator_name: str
    indicator_unit: str
    source_name: str
    source_note: str
    source_organization: str
    topics_snapshot: tuple[FrozenMacroTopicRef, ...] = ()
    provider_country_id: str
    iso2_code: str
    iso3_code: str
    geography_name: str
    region_name: str | None = None
    income_level_name: str | None = None
    page: int
    pages: int
    per_page: int
    provider_total: int
    provider_last_updated: str | None = None
    request_count: int
    acquisition_method: str
    authority_tier_snapshot: int
    critical_claim_eligible_snapshot: bool
    provider_capabilities_snapshot: tuple[str, ...] = ()
    fingerprint_version: int
    normalization_version: str
    status: str

    @field_validator(
        "requested_country_code",
        "source_id_snapshot",
        "indicator_name",
        "source_name",
        "provider_country_id",
        "iso2_code",
        "iso3_code",
        "geography_name",
        "acquisition_method",
    )
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="macro snapshot 字段")

    @field_validator("region_name", "income_level_name", "provider_last_updated")
    @classmethod
    def _v_optional(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("page", "pages", "per_page")
    @classmethod
    def _v_positive(cls, v: int) -> int:
        if v < 1:
            raise ValueError("page/pages/per_page 必须 >= 1")
        return v

    @field_validator("provider_total")
    @classmethod
    def _v_total(cls, v: int) -> int:
        if v < 0:
            raise ValueError("provider_total 必须 >= 0")
        return v

    @field_validator("request_count")
    @classmethod
    def _v_request_count(cls, v: int) -> int:
        if v < 1:
            raise ValueError("request_count 必须 >= 1")
        return v

    @field_validator("authority_tier_snapshot")
    @classmethod
    def _v_tier(cls, v: int) -> int:
        if v < 1 or v > 4:
            raise ValueError("authority_tier_snapshot 必须在 1..4")
        return v

    @field_validator("provider_capabilities_snapshot")
    @classmethod
    def _v_capabilities(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted({_strip(c, field="capability") for c in v}))

    @field_validator("normalization_version")
    @classmethod
    def _v_normalization_version(cls, v: str) -> str:
        return _strip(v, field="normalization_version")

    @field_validator("fingerprint_version")
    @classmethod
    def _v_fingerprint_version(cls, v: int) -> int:
        if v < 1:
            raise ValueError("fingerprint_version 必须 >= 1")
        return v

    @field_validator("status")
    @classmethod
    def _v_status(cls, v: str) -> str:
        v = _strip(v, field="status")
        if v not in _MACRO_SNAPSHOT_STATUSES:
            raise ValueError(f"status 必须为 {sorted(_MACRO_SNAPSHOT_STATUSES)}")
        return v

    @model_validator(mode="after")
    def _v_year_range(self) -> "FrozenMacroSnapshotDetail":
        if self.query_start_year > self.query_end_year:
            raise ValueError("query_start_year 必须 <= query_end_year")
        return self


class FrozenMacroSnapshotRef(StrictFrozenEvalModel):
    """一条 macro snapshot 的字节寻址引用（`snapshot_fingerprint` 是 semantic identity）。

    `payload_sha256` 是 Evaluation Bundle 冻结的 canonical payload bytes identity——
    独立证明 payload bytes 未被篡改，但**不**参与 duplicate identity（仍按
    `snapshot_fingerprint` 去重）。

    除 eval 层 identity 五字段外，还携带 **rehydration closure**（series / snapshot
    行 / observations / artifact_links / raw_artifacts）：materializer 始终填充，
    rehydrator 强制要求（缺失 → `EvalReplayIntegrityError`）。这些 closure 字段
    **不**进入 `compute_source_snapshot_fingerprint`（保持「domain macro
    fingerprint vs bundle 字节 identity」分离）。
    """

    snapshot_id: UUID
    series_id: UUID
    snapshot_fingerprint: str
    payload_sha256: str
    fetched_at: datetime
    series: FrozenMacroSeriesRef
    snapshot: FrozenMacroSnapshotDetail
    observations: tuple[FrozenMacroObservationRef, ...] = Field(min_length=1)
    artifact_links: tuple[FrozenMacroArtifactLinkRef, ...] = Field(min_length=1)
    raw_artifacts: tuple[FrozenMacroRawArtifactRef, ...] = Field(min_length=1)

    @field_validator("snapshot_fingerprint")
    @classmethod
    def _v_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="snapshot_fingerprint")

    @field_validator("payload_sha256")
    @classmethod
    def _v_payload_sha(cls, v: str) -> str:
        return _validate_sha256(v, field="payload_sha256")

    @model_validator(mode="after")
    def _reject_duplicate_closure(self) -> "FrozenMacroSnapshotRef":
        # 跨字段闭包：raw artifact_id 唯一；link snapshot_artifact_id 唯一；
        # 每个 link.artifact_id 必须存在于 raw_artifacts；每个 raw artifact 被 >=1 个
        # link 引用。允许多个 link → 同一 raw（role/page 只属于 link，不参与此处）。
        raw_ids = [ra.artifact_id for ra in self.raw_artifacts]
        if len(set(raw_ids)) != len(raw_ids):
            raise ValueError("raw_artifacts 存在重复 artifact_id")
        link_ids = [link.snapshot_artifact_id for link in self.artifact_links]
        if len(set(link_ids)) != len(link_ids):
            raise ValueError("artifact_links 存在重复 snapshot_artifact_id")
        raw_set = set(raw_ids)
        linked = {link.artifact_id for link in self.artifact_links}
        if not linked <= raw_set:
            raise ValueError("artifact_links 引用了不在 raw_artifacts 中的 artifact_id")
        if raw_set != linked:
            raise ValueError("raw_artifacts 存在未被任何 link 引用的 artifact_id")
        return self


class StructuredArtifactType(StrEnum):
    FINANCIAL_METRIC_OBSERVATION = "financial_metric_observation"
    RELATIVE_VALUATION_OBSERVATION = "relative_valuation_observation"
    RELATIVE_VALUATION_COMPARISON = "relative_valuation_comparison"


class FrozenStructuredArtifactRef(StrictFrozenEvalModel):
    """一条 structured artifact 的字节寻址引用（`artifact_fingerprint` 是 semantic identity）。

    `payload_sha256` 是 Evaluation Bundle 冻结的 canonical payload bytes identity——
    独立证明 payload bytes 未被篡改，但**不**参与 duplicate identity（仍按
    `(artifact_type, artifact_fingerprint)` 去重）。
    """

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


class FrozenSourceProviderRef(StrictFrozenEvalModel):
    """source provider registry 的一条冻结行（router / provenance 运行期读取的字段）。

    只冻结 routing 与 citation label 真正依赖的 semantic 字段：provider_key /
    display_name / enabled / capabilities。不冻结 authority_tier（router 排序后
    丢弃）、homepage_url / allowed_domains / acquisition_methods / exchange_scope
    / requires_api_key / critical_claim_eligible（运行期不读取）。
    """

    provider_key: str
    display_name: str
    enabled: bool
    capabilities: tuple[str, ...] = ()

    @field_validator("provider_key", "display_name")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="provider 元数据")

    @field_validator("capabilities")
    @classmethod
    def _v_capabilities(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # set-like 语义：strip + dedup + canonical sort（重复 capability 不影响
        # semantic identity；空白 capability 由 `_strip` 拒绝）。
        return tuple(sorted({_strip(c, field="capability") for c in v}))


class FrozenSourceSnapshot(StrictFrozenEvalModel):
    """三路评估的 frozen source snapshot manifest（document / macro / structured / provider）。

    collection 语义 unordered，duplicate identity 构造时拒绝；不含 raw bytes。
    """

    snapshot_schema_version: int = SNAPSHOT_SCHEMA_VERSION
    document_sources: tuple[FrozenDocumentSourceRef, ...] = ()
    macro_snapshots: tuple[FrozenMacroSnapshotRef, ...] = ()
    structured_artifacts: tuple[FrozenStructuredArtifactRef, ...] = ()
    source_providers: tuple[FrozenSourceProviderRef, ...] = ()

    @model_validator(mode="after")
    def _reject_duplicate_identity(self) -> "FrozenSourceSnapshot":
        # duplicate semantic identity：UUID 只做 provenance pointer，不决定
        # 「是否同一 frozen input」。document 用 content_sha256，macro 用
        # snapshot_fingerprint，structured 用 (artifact_type, artifact_fingerprint)，
        # provider 用 provider_key。
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
        seen_provider: set[str] = set()
        for ref in self.source_providers:
            if ref.provider_key in seen_provider:
                raise ValueError("duplicate source provider identity (provider_key)")
            seen_provider.add(ref.provider_key)
        return self


# ---------------------------------------------------------------- eval case


class FrozenCompanyIdentity(StrictFrozenEvalModel):
    """planner 运行期读取的 company 语义身份（不含内部 UUID / master-data 时间戳）。

    = `ResearchPlannerInputSnapshot` 的 company 子集：security_code / official_name /
    short_name(optional) / exchange / board / aliases。planner LLM prompt 的
    CompanyIdentitySnapshot 由这些字段确定性派生（short_name 前置去重进 aliases），
    因此冻结原始字段（含独立 short_name）即可完全重建 planner 输入。不冻结
    listing_status / listing_date / identity_source_* / source_updated_at（运行期
    不读取）。
    """

    security_code: str
    official_name: str
    short_name: str | None = None
    exchange: str
    board: str
    aliases: tuple[str, ...] = ()

    @field_validator("security_code")
    @classmethod
    def _v_security_code(cls, v: str) -> str:
        return _strip(v, field="security_code", max_len=_MAX_SECURITY_CODE_LENGTH)

    @field_validator("official_name", "exchange", "board")
    @classmethod
    def _v_nonempty(cls, v: str) -> str:
        return _strip(v, field="company identity 字段")

    @field_validator("short_name")
    @classmethod
    def _v_short_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        return v or None

    @field_validator("aliases")
    @classmethod
    def _v_aliases(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        # 与 production `_build_input_snapshot` 一致：去重 + 稳定排序。
        return tuple(sorted({_strip(a, field="alias") for a in v}))


class EvalCase(StrictFrozenEvalModel):
    """单个研究 case 的语义定义（不含 execution status / runtime id）。"""

    schema_version: int = EVAL_CASE_SCHEMA_VERSION
    case_id: str
    case_version: int = Field(ge=1)
    company_id: UUID
    company: FrozenCompanyIdentity
    research_question: str
    analysis_as_of: datetime
    tags: tuple[str, ...] = ()
    source_snapshot_fingerprint: str
    human_label_fingerprint: str | None = None

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _validate_slug(v)

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


class EvalDatasetCaseRef(StrictFrozenEvalModel):
    """dataset 里对某个 case 版本的引用。"""

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


class EvalDatasetManifest(StrictFrozenEvalModel):
    """frozen eval dataset 的 manifest（dataset 语义身份 + 有序 canonical case refs）。"""

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


class FinancialFactLabel(StrictFrozenEvalModel):
    """financial_number_accuracy 的人工 ground-truth（typed，非 free-text）。"""

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


class RiskTopicLabel(StrictFrozenEvalModel):
    """risk_topic_recall 的人工 ground-truth。"""

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


class ClaimSupportLabel(StrictFrozenEvalModel):
    """claim_support 的人工 ground-truth。"""

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


class MacroCausalLabel(StrictFrozenEvalModel):
    """macro_causal_error 的人工 ground-truth。"""

    label_type: Literal["macro_causal"] = "macro_causal"
    driver_code: str
    company_exposure_expected: bool
    causal_claim_allowed: bool

    @field_validator("driver_code")
    @classmethod
    def _v_driver_code(cls, v: str) -> str:
        return _strip(v, field="driver_code")


class HumanLabel(StrictFrozenEvalModel):
    """一个 case 的结构化人工标注（typed；annotation 不参与 machine ground-truth）。"""

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


class FrozenModelConfig(StrictFrozenEvalModel):
    """逐 variant 冻结的模型 / 参数（不含 API key）。"""

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


class EvalComponentVersion(StrictFrozenEvalModel):
    """单个 pipeline component 的冻结版本（`component_name` → `component_version`）。

    用于精确冻结 Full pipeline 多个 component 的真实版本（如 evidence_extractor:v2、
    audit:v1），弥补仅有 variant_version/prompt_version/retrieval_version/
    pipeline_version 的粒度不足。
    """

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


class EvalExecutionConfig(StrictFrozenEvalModel):
    """variant 执行配置（bounded deterministic settings + frozen model + component versions）。"""

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


class EvalExecutionSpec(StrictFrozenEvalModel):
    """「系统实际看到什么 + 以什么配置运行」（不含 label / metric registry / judge）。"""

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


class EvalScoringSpec(StrictFrozenEvalModel):
    """评分侧：绑定 variant 产出 + human label + metric registry + judge config。

    `variant_output_fingerprint` 是评分真正绑定的目标：normalized `EvalVariantOutput`
    的 semantic fingerprint（spec F）。旧字段 `execution_result_fingerprint` 没有
    formal producer，已弃用。`human_label_fingerprint` 与 `judge_config_fingerprint`
    均为可选（None canonical）：deterministic scoring spec（只跑 citation_validity /
    citation_coverage）无需 label 或 judge；label 缺失不改变 spec 有效性，None 在
    fingerprint 中规范为 `null`。
    """

    schema_version: int = EVAL_SCORING_SPEC_SCHEMA_VERSION
    variant_output_fingerprint: str
    human_label_fingerprint: str | None = None
    metric_registry_version: int = METRIC_REGISTRY_VERSION
    judge_config_fingerprint: str | None = None

    @field_validator("variant_output_fingerprint")
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


class EvalCitation(StrictFrozenEvalModel):
    """normalized output 的一条 citation。"""

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


class EvalClaim(StrictFrozenEvalModel):
    """normalized output 的一条 claim。"""

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


class EvalVariantOutput(StrictFrozenEvalModel):
    """三种 variant 最终都必须产出的 normalized structure（无 CoT / reasoning / key）。"""

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
