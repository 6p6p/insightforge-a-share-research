"""Snapshot materialization contracts (stage 7B.1.1B).

`EvalCaseMaterializationSpec`：一次 case materialization 的语义输入——case 语义
字段 + 三路 frozen input 的 selection（document source ids / macro snapshot ids /
structured artifact selection）。materializer 从真实 PG + RawArtifactStore 加载
并校验后，产出 frozen `EvalCase` + `FrozenSourceSnapshot` + source payloads。

`source_snapshot_fingerprint` **不是** caller 输入：由 materializer 从构建出的
`FrozenSourceSnapshot` 派生，保证 frozen input 身份与内容强一致。
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.eval.contracts import (
    EvalCase,
    FrozenSourceSnapshot,
    StructuredArtifactType,
    _strip,
    _validate_sha256,
    _validate_slug,
)


class StructuredArtifactSelection(BaseModel):
    """一条 structured artifact 的 materialization selection（artifact_type + id）。"""

    model_config = ConfigDict(frozen=True)

    artifact_type: StructuredArtifactType
    artifact_id: UUID


class EvalCaseMaterializationSpec(BaseModel):
    """case 语义输入 + 三路 selection（不含 source_snapshot_fingerprint）。"""

    model_config = ConfigDict(frozen=True)

    case_id: str
    case_version: int = Field(ge=1)
    company_id: UUID
    security_code: str
    research_question: str
    analysis_as_of: datetime
    tags: tuple[str, ...] = ()
    human_label_fingerprint: str | None = None
    document_source_ids: tuple[UUID, ...] = ()
    macro_snapshot_ids: tuple[UUID, ...] = ()
    structured_artifacts: tuple[StructuredArtifactSelection, ...] = ()

    @field_validator("case_id")
    @classmethod
    def _v_case_id(cls, v: str) -> str:
        return _validate_slug(v)

    @field_validator("security_code")
    @classmethod
    def _v_security_code(cls, v: str) -> str:
        return _strip(v, field="security_code", max_len=32)

    @field_validator("research_question")
    @classmethod
    def _v_question(cls, v: str) -> str:
        return _strip(v, field="research_question", max_len=4000)

    @field_validator("tags")
    @classmethod
    def _v_tags(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_strip(t, field="tag", max_len=64) for t in v)

    @field_validator("human_label_fingerprint")
    @classmethod
    def _v_label_fp(cls, v: str | None) -> str | None:
        if v is not None:
            return _validate_sha256(v, field="human_label_fingerprint")
        return v


@dataclass(frozen=True)
class MaterializedEvalCase:
    """materialize_case 的产物：frozen contracts + source payloads（内存态，不落盘）。

    - `document_blobs`：content_sha256 → raw bytes（已重新 SHA-256 校验）。
    - `macro_payloads`：snapshot_fingerprint → payload。
    - `macro_raw_blobs`：content_sha256 → macro 原始响应 raw bytes（已重新
      SHA-256 校验；与 document blob 共用同一 content-addressed 布局）。
    - `structured_payloads`：(artifact_type, artifact_fingerprint) → payload。
    """

    case: EvalCase
    snapshot: FrozenSourceSnapshot
    document_blobs: dict[str, bytes]
    macro_payloads: dict[str, dict[str, Any]]
    macro_raw_blobs: dict[str, bytes]
    structured_payloads: dict[tuple[StructuredArtifactType, str], dict[str, Any]]
