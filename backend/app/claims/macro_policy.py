"""Shared pure macro transmission policy helpers (stage 4C.1B).

MacroClaimService 与 MacroAnalysisService **共用**本模块的纯函数，**禁止重复
实现 no-lookahead / driver 资格策略**（也不得调用 MacroClaimService 私有方法）：
- **driver 资格**：macro_observation，或经过明确筛选的 external event document
  Evidence（news_article + evidence_type ∈ {event, fact, statement}）——4C.1A
  v2 政策，4C.1B 原样复用；
- **information availability（no-lookahead）**：macro 卡用 MacroDatasetSnapshot.
  fetched_at（系统最晚何时已取得该观测值）；document 卡用 SourceRecord.
  published_at，为 NULL 时保守 fallback 到 acquired_at；**绝不用
  reporting_period_end / normalized_period_start**（经济期间 ≠ 信息可得时间）。

纯函数（输入真实 provenance 快照值，不访问 DB）；缺失 provenance 由调用方
决定错误映射（数据损坏 → IntegrityError；存在但无可用时间 → TemporalInsufficient）。
"""

from datetime import datetime

from app.domain.source_records import SourceDocumentType
from app.evidence.contracts import EvidenceOrigin, EvidenceType

# 外部 event document driver 允许的 EvidenceType（4C.1A v2 政策）。排除
# context（背景不是 driver）与 metric（结构化数值优先 MacroObservation）。
_DOCUMENT_DRIVER_EVIDENCE_TYPES = frozenset(
    (EvidenceType.EVENT.value, EvidenceType.FACT.value, EvidenceType.STATEMENT.value)
)


def driver_evidence_eligible(
    *,
    origin_type: str,
    evidence_type: str,
    source_document_type: str | None,
) -> bool:
    """macro_driver 资格（v2/v3 政策）。

    - origin_type=macro_observation → True（source_document_type 无关）；
    - origin_type=document_chunk → 需 SourceRecord.document_type=news_article
      且 evidence_type ∈ {event, fact, statement}；
    - 其他 origin / 缺 source → False。
    """
    if origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
        return True
    if origin_type == EvidenceOrigin.DOCUMENT_CHUNK.value:
        return (
            source_document_type == SourceDocumentType.NEWS_ARTICLE.value
            and evidence_type in _DOCUMENT_DRIVER_EVIDENCE_TYPES
        )
    return False


def resolve_availability(
    *,
    origin_type: str,
    snapshot_fetched_at: datetime | None,
    source_published_at: datetime | None,
    source_acquired_at: datetime | None,
) -> datetime | None:
    """v2/v3 information availability（真实 provenance，不伪造缺失日期）。

    - macro 卡：MacroDatasetSnapshot.fetched_at——系统最晚何时已取得该观测值
      （provider release 时间未结构化捕获，**绝不用 period /
      normalized_period_start 冒充"何时可知"）；
    - document 卡：SourceRecord.published_at（真实发布时间）；为 NULL 时用
      acquired_at 作为保守 fallback；**绝不用 reporting_period_end**。

    provenance 缺失（snapshot_fetched_at / 两个 source 时间全为 None）→ 返回
    None，由调用方决定错误映射。
    """
    if origin_type == EvidenceOrigin.MACRO_OBSERVATION.value:
        return snapshot_fetched_at
    if source_published_at is not None:
        return source_published_at
    return source_acquired_at
