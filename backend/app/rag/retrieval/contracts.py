"""Retrieval contracts (stage 3B.2): RetrievalQuery / RetrievalHit / Chroma where.

角色分工：
- PostgreSQL = Source of Truth：RetrievalHit 的全部字段从 PG hydrate
  （DocumentChunk → ChunkSet → ParsedSource → SourceRecord → RawArtifact
  provenance），Chroma 只返回 candidate chunk_id + distance，绝不作为正文来源。
- Chroma = 可重建 derived index：只接受确定性 where（chunk_set_id $in
  eligible ids + RetrievalQuery filters），**不支持任意用户自定义 Chroma where
  JSON**。
- 排序只使用 Chroma cosine distance；**不做 similarity threshold / reranker /
  MMR / BM25 混合 / LLM relevance judge**。绝对距离只作为 retrieval diagnostic，
  不解释成置信度（字段名 `distance`，禁止叫 confidence/probability）。

本模块冻结：
- RetrievalQuery：company_id 必填、query_text trim 后非空（max 1000 字符）、
  top_k 默认 10（1..50）、可选 filters（source_ids / provider_keys /
  document_types / authority_tiers / critical_claim_eligible_only /
  published_from/to / reporting_period_from/to）；时间必须 timezone-aware，
  from <= to。
- RetrievalHit：read model（rank / chunk_id / chunk_set_id / parsed_source_id /
  source_id / company_id / text / distance / provider_key / document_type /
  source_title / source_url / published_at / reporting_period_end /
  authority_tier / critical_claim_eligible / chunk_ordinal / locator_refs）。
- build_chroma_where：把 eligible chunk_set_ids + filters 组装成单个 $and where。
"""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from uuid import UUID

from app.rag.retrieval.errors import RetrievalQueryError

MAX_QUERY_TEXT_CHARS = 1000
DEFAULT_TOP_K = 10
MAX_TOP_K = 50

RetrievalLocatorRefs = list  # JSONB 数组（nested JSON 只从 PG hydrate，不进 Chroma）


@dataclass(frozen=True)
class RetrievalQuery:
    """一次语义检索的输入（构造时校验，不可变）。

    - company_id：必填，查询只在该公司内检索（PG 侧 company 过滤 +
      Chroma 侧 chunk_set_id 白名单双闸）；
    - query_text：trim 后非空、≤ 1000 字符；embed_query 加 BGE query
      instruction（禁止 silent truncation）；
    - top_k：默认 10，范围 1..50；
    - filters 全部可选；空 list 视为无过滤；时间必须 timezone-aware，
      from <= to。
    """

    company_id: UUID
    query_text: str
    top_k: int = DEFAULT_TOP_K
    source_ids: list[UUID] | None = None
    provider_keys: list[str] | None = None
    document_types: list[str] | None = None
    authority_tiers: list[int] | None = None
    critical_claim_eligible_only: bool = False
    published_from: datetime | None = None
    published_to: datetime | None = None
    reporting_period_from: date | None = None
    reporting_period_to: date | None = None

    def __post_init__(self) -> None:
        if isinstance(self.company_id, bool) or not isinstance(self.company_id, UUID):
            raise RetrievalQueryError("company_id 必须是 UUID")
        text = self.query_text.strip()
        if not text:
            raise RetrievalQueryError("query_text 不能为空（trim 后）")
        if len(text) > MAX_QUERY_TEXT_CHARS:
            raise RetrievalQueryError(f"query_text 不能超过 {MAX_QUERY_TEXT_CHARS} 字符")
        if isinstance(self.top_k, bool) or not isinstance(self.top_k, int):
            raise RetrievalQueryError("top_k 必须是 int")
        if not 1 <= self.top_k <= MAX_TOP_K:
            raise RetrievalQueryError(f"top_k 必须在 1..{MAX_TOP_K}")
        # 空 filter list 视为未提供（避免 "" 与 [] 的二义性）。
        for name in ("source_ids", "provider_keys", "document_types", "authority_tiers"):
            value = getattr(self, name)
            if value is not None and len(value) == 0:
                object.__setattr__(self, name, None)
        for name in ("published_from", "published_to"):
            value = getattr(self, name)
            if value is not None and value.tzinfo is None:
                raise RetrievalQueryError(f"{name} 必须是 timezone-aware datetime")
        if (
            self.published_from is not None
            and self.published_to is not None
            and self.published_from > self.published_to
        ):
            raise RetrievalQueryError("published_from 不能晚于 published_to")
        if (
            self.reporting_period_from is not None
            and self.reporting_period_to is not None
            and self.reporting_period_from > self.reporting_period_to
        ):
            raise RetrievalQueryError("reporting_period_from 不能晚于 reporting_period_to")
        object.__setattr__(self, "query_text", text)


@dataclass(frozen=True)
class RetrievalHit:
    """一个候选 Chunk 及其证据链定位（read model，全部从 PG hydrate）。

    `distance` 是 Chroma cosine distance（越小越相似），**只作为检索诊断**，
    不解释成置信度 / probability。
    """

    rank: int
    chunk_id: UUID
    chunk_set_id: UUID
    parsed_source_id: UUID
    source_id: UUID
    company_id: UUID
    text: str
    distance: float
    provider_key: str
    document_type: str
    source_title: str | None
    source_url: str | None
    published_at: datetime | None
    reporting_period_end: date | None
    authority_tier: int
    critical_claim_eligible: bool
    chunk_ordinal: int
    locator_refs: RetrievalLocatorRefs


def date_to_epoch(value: date) -> int:
    """reporting_period_end（date）→ 当日 00:00 UTC epoch（与 index metadata 一致）。"""
    return int(datetime.combine(value, time.min, tzinfo=UTC).timestamp())


def build_chroma_where(*, chunk_set_ids: list[UUID], query: RetrievalQuery) -> dict:
    """把 eligible chunk_set_ids + RetrievalQuery filters 组装成 Chroma where。

    - 至少含 `chunk_set_id: {"$in": eligible}`（company 隔离的白名单闸）；
    - 其余 filters 组合成单个 `$and`；**不支持任意用户自定义 where JSON**；
    - published_at / reporting_period_end 用 epoch 比较：仅当记录有该 epoch
      metadata 时参与比较（无值的记录在时间过滤下被排除，NULL 不伪造）。
    """
    clauses: list[dict] = [
        {"chunk_set_id": {"$in": [str(chunk_set_id) for chunk_set_id in chunk_set_ids]}}
    ]
    if query.source_ids:
        clauses.append({"source_id": {"$in": [str(sid) for sid in query.source_ids]}})
    if query.provider_keys:
        clauses.append({"provider_key": {"$in": list(query.provider_keys)}})
    if query.document_types:
        clauses.append({"document_type": {"$in": list(query.document_types)}})
    if query.authority_tiers:
        clauses.append({"authority_tier": {"$in": list(query.authority_tiers)}})
    if query.critical_claim_eligible_only:
        clauses.append({"critical_claim_eligible": True})
    if query.published_from is not None:
        clauses.append({"published_at_epoch": {"$gte": int(query.published_from.timestamp())}})
    if query.published_to is not None:
        clauses.append({"published_at_epoch": {"$lte": int(query.published_to.timestamp())}})
    if query.reporting_period_from is not None:
        clauses.append(
            {"reporting_period_end_epoch": {"$gte": date_to_epoch(query.reporting_period_from)}}
        )
    if query.reporting_period_to is not None:
        clauses.append(
            {"reporting_period_end_epoch": {"$lte": date_to_epoch(query.reporting_period_to)}}
        )
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}
