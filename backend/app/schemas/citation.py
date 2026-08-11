"""Citation navigation read-only contracts (Stage 6B.2).

Report → click citation → Evidence → Claim relation → Source / locator，以及
Claim citation → evidence relation list。**只读**投影：不包含任何写入契约，
不暴露 fingerprint / storage_key / prompt / reasoning_content / raw provider
JSON。

`provenance` 是 discriminated union（按 `origin_type`）：
- `document_chunk`：EvidenceCard → DocumentChunk → ChunkSet → ParsedSource →
  SourceRecord → RawArtifact 全链 verified 投影（quote / context / locator /
  source 元数据）；
- `macro_observation`：EvidenceCard → MacroObservation → MacroDatasetSnapshot →
  MacroSeries → SourceProvider + MacroSnapshotArtifact links → RawArtifact 全链
  verified 投影。

字段按现有真实 models 命名，**不得伪造 SourceRecord**（macro 的 source_id
恒为 NULL）；`context_text` 只返回当前 Chunk 安全纯文本上下文（≤5000 chars），
不返回整个原文。
"""

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, Field

# ------------------------------------------------------------------ locator


class CitationLocator(BaseModel):
    """quote 级 locator（EvidenceCard.locator_refs 投影，原样保留类型字段）。

    `locator_type` ∈ {html_dom, pdf_page}，各类型可选字段对应原 locator dict
    （html_dom: ordinal/tag/xpath/element_id；pdf_page: page_number/line_index/
    bbox/page_width/page_height）；`block_ordinal` / `char_start` / `char_end`
    是 EvidenceCard.locator_refs 的公共 block 索引字段。
    """

    locator_type: str
    block_ordinal: int | None = None
    char_start: int | None = None
    char_end: int | None = None
    # html_dom 专属字段
    ordinal: int | None = None
    tag: str | None = None
    xpath: str | None = None
    element_id: str | None = None
    # pdf_page 专属字段
    page_number: int | None = None
    line_index: int | None = None
    bbox: list[float] | None = None
    page_width: float | None = None
    page_height: float | None = None


# ------------------------------------------------------------------ document provenance


class DocumentProvenance(BaseModel):
    """document_chunk 证据的 verified provenance（spec K）。

    `context_text` 只返回当前 Chunk 安全纯文本上下文（≤5000 chars），**不返回
    整个原文**；`locator` 是首条 quote 级 ref，`locator_refs` 为全部 quote 级
    refs（页 / 块定位）。
    """

    origin_type: Literal["document_chunk"]
    source_id: UUID
    provider_key: str
    provider_label: str
    title: str
    source_url: str
    published_at: datetime | None = None
    authority_tier: int
    document_type: str | None = None
    raw_artifact_id: UUID
    media_type: str
    parsed_source_id: UUID
    chunk_id: UUID
    locator: CitationLocator | None = None
    locator_refs: list[CitationLocator] = []
    context_text: str
    quote_text: str | None = None


# ------------------------------------------------------------------ macro provenance


class MacroArtifactLink(BaseModel):
    """macro snapshot → raw artifact 归档链接（role + page + 归档元数据）。"""

    role: str
    page: int | None = None
    artifact_id: UUID
    media_type: str
    fetched_at: datetime


class MacroProvenance(BaseModel):
    """macro_observation 证据的 verified provenance（spec K）。

    字段按真实 Macro models 命名（MacroObservation / MacroDatasetSnapshot /
    MacroSeries / SourceProvider / MacroSnapshotArtifact / RawArtifact），
    **不得伪造 SourceRecord**。`value` 为 Decimal → str 序列化（避免浮点精度
    歧义）；`is_missing=true` 时 `value=None`。
    """

    origin_type: Literal["macro_observation"]
    observation_id: UUID
    period: str
    value: str | None = None
    is_missing: bool
    snapshot_id: UUID
    fetched_at: datetime
    series_id: UUID
    indicator: str
    geography: str
    provider_key: str
    provider_label: str
    authority_tier: int
    source_name: str | None = None
    source_organization: str | None = None
    raw_artifact_id: UUID | None = None
    media_type: str | None = None
    artifact_links: list[MacroArtifactLink] = []


EvidenceProvenance = Annotated[
    DocumentProvenance | MacroProvenance,
    Field(discriminator="origin_type"),
]


# ------------------------------------------------------------------ evidence citation


class EvidenceCitationClaimRelation(BaseModel):
    """evidence 对 canonical 综合 input Claim 的关系（supports / contradicts / context）。

    只投影当前任务 canonical synthesis input Claims 的关系，**不混入其他任务 /
    旧 synthesis 的 claim**（spec J：不暴露跨任务存在性）。
    """

    claim_id: UUID
    claim_statement: str
    relation: str


class EvidenceCitationPayload(BaseModel):
    """evidence 头部投影（spec K）。"""

    evidence_card_id: UUID
    statement: str
    quote_text: str | None = None
    evidence_type: str
    origin_type: str


class EvidenceCitationResponse(BaseModel):
    evidence: EvidenceCitationPayload
    claim_relations: list[EvidenceCitationClaimRelation] = []
    provenance: EvidenceProvenance


# ------------------------------------------------------------------ claim citation


class ClaimCitationEvidenceRelation(BaseModel):
    """claim ↔ evidence 关系（relation 保留 supports / contradicts / context，不压平）。"""

    evidence_card_id: UUID
    evidence_statement: str
    relation: str


class ClaimCitationResponse(BaseModel):
    claim_id: UUID
    statement: str
    domain: str
    kind: str
    confidence: str
    importance: str
    evidence_relations: list[ClaimCitationEvidenceRelation] = []
