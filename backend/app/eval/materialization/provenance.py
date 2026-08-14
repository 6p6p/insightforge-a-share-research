"""Structured artifact provenance builders (stage 7B.1.4C.3).

从真实 PG 行构建 **stable semantic provenance**（纯函数，不访问 DB）：

- `build_evidence_match(card, source, artifact)`：target-company 观测的 evidence
  **匹配** provenance——只冻结能定位 attempt 重新生成 EvidenceCard 的语义键
  （source document content_sha256 + evidence statement + quote），**不**冻结
  旧卡内容（本 attempt 的卡由 retrieval → extraction 重新生成）；
- `build_replay_evidence(card, source, artifact, company)`：peer 观测的 evidence
  **replay** provenance——peer 公司不在 frozen snapshot 中，无文档可重新提取，
  必须冻结完整旧卡内容 + source 语义字段 + 公司身份，由 remap 确定性重建
  （peer replay 卡不进入 target 公司的证据链，仅供 comparison 校验引用）。

`replay_evidence` 的 `evidence_statement` / `quote_text` 来自真实提取结果
（**不伪造**）；parsed/chunk 等 persistence-only 脚手架由 remap 用确定性 policy
补全（与 rehydrator 的 `replay_v1` 同哲学）。
"""

from app.db.models.company import CompanyModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_record import SourceRecordModel

EVIDENCE_PROVENANCE_SCHEMA_VERSION = 1


def build_observation_source_provenance(evidence_match: dict) -> dict:
    """observation payload 的 provenance envelope（`source_evidence` 匹配键）。"""
    return {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA_VERSION,
        "source_evidence": evidence_match,
    }


def build_evidence_match(
    card: EvidenceCardModel,
    source: SourceRecordModel,
    artifact: RawArtifactModel,
) -> dict:
    """target-company evidence 匹配键（attempt 重新生成卡后按此定位）。"""
    return {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA_VERSION,
        "content_sha256": artifact.content_sha256,
        "evidence_statement": card.evidence_statement,
        "quote_text": card.quote_text,
    }


def build_replay_evidence(
    card: EvidenceCardModel,
    source: SourceRecordModel,
    artifact: RawArtifactModel,
    company: CompanyModel,
) -> dict:
    """peer evidence 完整 replay 数据（公司 + source + 卡内容，全部语义冻结）。"""
    return {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA_VERSION,
        "company": {
            "exchange": company.exchange,
            "security_code": company.security_code,
            "official_name": company.official_name,
            "short_name": company.short_name,
            "board": company.board,
        },
        "source": {
            "provider_key": source.provider_key,
            "document_type": source.document_type,
            "media_type": artifact.media_type,
            "title": source.title,
            "source_url": source.source_url,
            "published_at": source.published_at.isoformat() if source.published_at else None,
            "acquired_at": source.acquired_at.isoformat(),
            "reporting_period_end": (
                source.reporting_period_end.isoformat() if source.reporting_period_end else None
            ),
            "authority_tier_snapshot": source.authority_tier_snapshot,
            "critical_claim_eligible_snapshot": source.critical_claim_eligible_snapshot,
            "provider_capabilities_snapshot": list(source.provider_capabilities_snapshot),
            "acquisition_method": source.acquisition_method,
            "status": source.status,
        },
        "evidence": {
            "content_sha256": artifact.content_sha256,
            "research_question": card.research_question,
            "research_question_sha256": card.research_question_sha256,
            "evidence_statement": card.evidence_statement,
            "evidence_type": card.evidence_type,
            "quote_start": card.quote_start,
            "quote_end": card.quote_end,
            "quote_text": card.quote_text,
            "quote_sha256": card.quote_sha256,
            "locator_refs": list(card.locator_refs),
            "provider_key": card.provider_key,
            "source_published_at": (
                card.source_published_at.isoformat() if card.source_published_at else None
            ),
            "reporting_period_end": (
                card.reporting_period_end.isoformat() if card.reporting_period_end else None
            ),
            "authority_tier_snapshot": card.authority_tier_snapshot,
            "critical_claim_eligible_snapshot": card.critical_claim_eligible_snapshot,
            "extractor_name": card.extractor_name,
            "extractor_version": card.extractor_version,
            "extractor_model_id": card.extractor_model_id,
            "extractor_confidence": card.extractor_confidence,
            "evidence_schema_version": card.evidence_schema_version,
            "evidence_fingerprint": card.evidence_fingerprint,
        },
    }


def build_observation_provenance(
    *,
    metric_code: str,
    metric_as_of: str,
    source_value_text: str,
    metric_value: str,
    evidence: dict,
) -> dict:
    """一条 valuation observation 的 provenance（comparison 的 target/peer 用）。"""
    return {
        "schema_version": EVIDENCE_PROVENANCE_SCHEMA_VERSION,
        "metric_code": metric_code,
        "metric_as_of": metric_as_of,
        "source_value_text": source_value_text,
        "metric_value": metric_value,
        "evidence": evidence,
    }
