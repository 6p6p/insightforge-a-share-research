"""Materializer payload projections (stage 7B.1.1B).

纯函数：从真实 PG model 行投影出 Evaluation Bundle 的 frozen payload dict
（全部 JSON-serializable：UUID / date / datetime / Decimal 已序列化为 str /
None）。**不访问 DB、不计算 domain fingerprint**——宏观 / 估值 fingerprint 由
domain 模块负责；本模块只做 payload 投影 + envelope + payload_sha256。

envelope 契约（与 bundle writer 对齐）：
- macro payload 必须含 `snapshot_fingerprint` 键；
- structured payload 必须含 `artifact_type` + `artifact_fingerprint` 键。
"""

import hashlib
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.db.models.financial_metric_observation import FinancialMetricObservationModel
from app.db.models.macro_dataset_snapshot import MacroDatasetSnapshotModel
from app.db.models.macro_observation import MacroObservationModel
from app.db.models.macro_series import MacroSeriesModel
from app.db.models.valuation_metric_observation import ValuationMetricObservationModel
from app.eval.canonical import canonical_json_bytes
from app.eval.contracts import StructuredArtifactType
from app.valuation.comparison_service import VerifiedComparison

PAYLOAD_SCHEMA_VERSION = 2  # v2: structured payload 携带 stable semantic provenance


def payload_sha256(payload: dict[str, Any]) -> str:
    """canonical JSON bytes 的 SHA-256（与 bundle writer 的 payload identity 一致）。"""
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _iso(value: date | datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _dec(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def build_macro_payload(
    snapshot: MacroDatasetSnapshotModel,
    series: MacroSeriesModel,
    observations: list[MacroObservationModel],
) -> dict[str, Any]:
    """投影 frozen macro payload（envelope 含 `snapshot_fingerprint`）。

    本模块只做 payload 投影，不重算 snapshot fingerprint（重算由
    `MacroPersistenceService.verify_snapshot_integrity` 完成）；`snapshot_fingerprint`
    作为 content identity 由 persisted 行提供。
    """
    return {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "snapshot_fingerprint": snapshot.snapshot_fingerprint,
        "fetched_at": _iso(snapshot.fetched_at),
        "series": {
            "provider_key": series.provider_key,
            "source_id": series.source_id,
            "external_indicator_id": series.external_indicator_id,
            "geography_type": series.geography_type,
            "geography_code": series.geography_code,
            "frequency": series.frequency,
        },
        "indicator": {
            "name": snapshot.indicator_name,
            "unit": snapshot.indicator_unit,
            "source_name": snapshot.source_name,
            "source_note": snapshot.source_note,
            "source_organization": snapshot.source_organization,
            "topics": snapshot.topics_snapshot,
        },
        "geography": {
            "provider_country_id": snapshot.provider_country_id,
            "iso2_code": snapshot.iso2_code,
            "iso3_code": snapshot.iso3_code,
            "name": snapshot.geography_name,
            "region_name": snapshot.region_name,
            "income_level_name": snapshot.income_level_name,
        },
        "observations": [
            {
                "period": o.period,
                "normalized_period_start": _iso(o.normalized_period_start),
                "value": _dec(o.value_numeric),
                "is_missing": o.is_missing,
                "decimal_scale": o.decimal_scale,
                "observation_status": o.observation_status,
            }
            for o in observations
        ],
    }


def build_financial_metric_payload(
    row: FinancialMetricObservationModel,
    artifact_fingerprint: str,
    provenance: dict | None = None,
) -> dict[str, Any]:
    """投影 frozen financial metric observation payload（envelope 含 identity）。

    `provenance`（v2）：stable semantic evidence 匹配键（content_sha256 +
    statement + quote），供 rehydration 后把观测重新绑定到 attempt 新 EvidenceCard。
    """
    payload: dict[str, Any] = {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "artifact_type": StructuredArtifactType.FINANCIAL_METRIC_OBSERVATION.value,
        "artifact_fingerprint": artifact_fingerprint,
        "metric_observation_id": str(row.metric_observation_id),
        "company_id": str(row.company_id),
        "source_evidence_card_id": str(row.source_evidence_card_id),
        "metric_code": row.metric_code,
        "statement_scope": row.statement_scope,
        "period_start": _iso(row.period_start),
        "period_end": _iso(row.period_end),
        "period_kind": row.period_kind,
        "source_value_text": row.source_value_text,
        "raw_value": _dec(row.raw_value),
        "raw_unit": row.raw_unit,
        "normalized_value_cny": _dec(row.normalized_value_cny),
        "metric_schema_version": row.metric_schema_version,
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return payload


def build_valuation_observation_payload(
    row: ValuationMetricObservationModel,
    artifact_fingerprint: str,
    provenance: dict | None = None,
) -> dict[str, Any]:
    """投影 frozen valuation observation payload（envelope 含 identity）。"""
    payload: dict[str, Any] = {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "artifact_type": StructuredArtifactType.RELATIVE_VALUATION_OBSERVATION.value,
        "artifact_fingerprint": artifact_fingerprint,
        "valuation_observation_id": str(row.valuation_observation_id),
        "company_id": str(row.company_id),
        "source_evidence_card_id": str(row.source_evidence_card_id),
        "metric_code": row.metric_code,
        "metric_as_of": _iso(row.metric_as_of),
        "source_value_text": row.source_value_text,
        "metric_value": _dec(row.metric_value),
        "valuation_observation_schema_version": row.valuation_observation_schema_version,
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return payload


def build_comparison_payload(
    verified: VerifiedComparison,
    provenance: dict | None = None,
) -> dict[str, Any]:
    """投影 frozen relative valuation comparison payload（envelope 含 identity）。

    注意：`VerifiedComparison` 不暴露 `comparison_schema_version`（只暴露
    `formula_version`）；schema version 已编码进 `comparison_fingerprint`，不重复。
    """
    payload: dict[str, Any] = {
        "schema_version": PAYLOAD_SCHEMA_VERSION,
        "artifact_type": StructuredArtifactType.RELATIVE_VALUATION_COMPARISON.value,
        "artifact_fingerprint": verified.comparison_fingerprint,
        "comparison_id": str(verified.comparison_id),
        "target_company_id": str(verified.target_company_id),
        "target_observation_id": str(verified.target_observation_id),
        "metric_code": verified.metric_code,
        "metric_as_of": _iso(verified.metric_as_of),
        "analysis_as_of": _iso(verified.analysis_as_of),
        "comparison_method": verified.comparison_method,
        "formula_version": verified.formula_version,
        "peer_count": verified.peer_count,
        "peer_median": _dec(verified.peer_median),
        "peer_min": _dec(verified.peer_min),
        "peer_max": _dec(verified.peer_max),
        "premium_discount_to_median": _dec(verified.premium_discount_to_median),
        "peer_companies": [str(c) for c in verified.peer_companies],
        "peer_observation_ids": [str(o) for o in verified.peer_observation_ids],
    }
    if provenance is not None:
        payload["provenance"] = provenance
    return payload
