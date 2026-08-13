"""Evaluation fingerprints (stage 7B.1.0).

统一复用项目 canonical JSON SHA-256 idiom：
`json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
→ SHA-256 hex。

冻结规则：
- 排除 runtime identity：created_at / runtime UUID / DB execution id / API key /
  wall-clock latency。
- Source Snapshot fp 用**内容 hash + semantic metadata**，不用 UUID-only identity
  （source_record_id / artifact_id / snapshot_id / series_id 等 DB UUID 变化不改变
  semantic identity）。
- HumanLabel fp 排除 `annotation`（free-text 不进 machine ground-truth）。
- EvalCase fp 排除 `human_label_fingerprint`（label 属 scoring 侧，不进 execution
  input）。
- unordered collection 一律 canonical sort，保证 tuple 插入顺序不影响 fingerprint。
"""

import hashlib
from datetime import date, datetime
from decimal import Decimal

from app.eval.canonical import canonical_json_str
from app.eval.contracts import (
    EvalCase,
    EvalDatasetManifest,
    EvalExecutionConfig,
    EvalExecutionSpec,
    EvalScoringSpec,
    EvalVariantOutput,
    FrozenSourceSnapshot,
    HumanLabel,
)


def _sha256_hex(canonical: str) -> str:
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _iso(value: datetime | date | None) -> str | None:
    return value.isoformat() if value is not None else None


def _decimal(value: Decimal) -> str:
    return str(value)


def compute_source_snapshot_fingerprint(snapshot: FrozenSourceSnapshot) -> str:
    """snapshot semantic identity = 内容 hash + semantic metadata（不含 UUID）。"""
    document_sources = sorted(
        [
            {
                "content_sha256": ref.content_sha256,
                "provider_key": ref.provider_key,
                "document_type": ref.document_type,
                "media_type": ref.media_type,
                "published_at": _iso(ref.published_at),
                "reporting_period_start": _iso(ref.reporting_period_start),
                "reporting_period_end": _iso(ref.reporting_period_end),
            }
            for ref in snapshot.document_sources
        ],
        key=canonical_json_str,
    )
    macro_snapshots = sorted(
        [
            {
                "snapshot_fingerprint": ref.snapshot_fingerprint,
                "payload_sha256": ref.payload_sha256,
                "fetched_at": _iso(ref.fetched_at),
            }
            for ref in snapshot.macro_snapshots
        ],
        key=canonical_json_str,
    )
    structured_artifacts = sorted(
        [
            {
                "artifact_type": ref.artifact_type.value,
                "artifact_fingerprint": ref.artifact_fingerprint,
                "payload_sha256": ref.payload_sha256,
            }
            for ref in snapshot.structured_artifacts
        ],
        key=canonical_json_str,
    )
    payload = {
        "snapshot_schema_version": snapshot.snapshot_schema_version,
        "document_sources": document_sources,
        "macro_snapshots": macro_snapshots,
        "structured_artifacts": structured_artifacts,
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_eval_case_fingerprint(case: EvalCase) -> str:
    """case 语义身份（排除 human_label_fingerprint：label 属 scoring 侧）。"""
    payload = {
        "schema_version": case.schema_version,
        "case_id": case.case_id,
        "case_version": case.case_version,
        "company_id": str(case.company_id),
        "security_code": case.security_code,
        "research_question": case.research_question,
        "analysis_as_of": _iso(case.analysis_as_of),
        "tags": sorted(case.tags),
        "source_snapshot_fingerprint": case.source_snapshot_fingerprint,
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_dataset_fingerprint(manifest: EvalDatasetManifest) -> str:
    """dataset 语义身份 + ordered canonical case refs（排除 description）。"""
    cases = sorted(
        [
            {
                "case_id": ref.case_id,
                "case_version": ref.case_version,
                "case_fingerprint": ref.case_fingerprint,
            }
            for ref in manifest.cases
        ],
        key=lambda d: (d["case_id"], d["case_version"]),
    )
    payload = {
        "schema_version": manifest.schema_version,
        "dataset_id": manifest.dataset_id,
        "dataset_version": manifest.dataset_version,
        "cases": cases,
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_human_label_fingerprint(label: HumanLabel) -> str:
    """typed label 语义身份（排除 free-text annotation）。"""
    payload = {
        "schema_version": label.schema_version,
        "case_id": label.case_id,
        "case_version": label.case_version,
        "label_version": label.label_version,
        "financial_facts": sorted(
            [
                {
                    "metric_code": f.metric_code,
                    "period": f.period,
                    "scope": f.scope,
                    "unit": f.unit,
                    "expected_value": _decimal(f.expected_value),
                    "absolute_tolerance": _decimal(f.absolute_tolerance),
                    "relative_tolerance": _decimal(f.relative_tolerance),
                }
                for f in label.financial_facts
            ],
            key=canonical_json_str,
        ),
        "risk_topics": sorted(
            [
                {
                    "risk_code": r.risk_code,
                    "required": r.required,
                    "acceptable_aliases": sorted(r.acceptable_aliases),
                }
                for r in label.risk_topics
            ],
            key=canonical_json_str,
        ),
        "claim_support_labels": sorted(
            [
                {
                    "claim_label_id": c.claim_label_id,
                    "expected_support_status": c.expected_support_status.value,
                    "related_source_fingerprints": sorted(c.related_source_fingerprints),
                }
                for c in label.claim_support_labels
            ],
            key=canonical_json_str,
        ),
        "macro_causal_labels": sorted(
            [
                {
                    "driver_code": m.driver_code,
                    "company_exposure_expected": m.company_exposure_expected,
                    "causal_claim_allowed": m.causal_claim_allowed,
                }
                for m in label.macro_causal_labels
            ],
            key=canonical_json_str,
        ),
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_execution_config_fingerprint(config: EvalExecutionConfig) -> str:
    payload = {
        "config_schema_version": config.config_schema_version,
        "variant_id": config.variant_id.value,
        "model": {
            "provider": config.model.provider,
            "model_id": config.model.model_id,
            "thinking_enabled": config.model.thinking_enabled,
            "temperature": _decimal(config.model.temperature),
            "max_output_tokens": config.model.max_output_tokens,
            "structured_output": config.model.structured_output,
        },
        "variant_version": config.variant_version,
        "prompt_version": config.prompt_version,
        "retrieval_version": config.retrieval_version,
        "pipeline_version": config.pipeline_version,
        "retrieval_top_k": config.retrieval_top_k,
        "component_versions": sorted(
            [
                {
                    "component_name": c.component_name,
                    "component_version": c.component_version,
                }
                for c in config.component_versions
            ],
            key=lambda d: d["component_name"],
        ),
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_execution_spec_fingerprint(spec: EvalExecutionSpec) -> str:
    payload = {
        "schema_version": spec.schema_version,
        "case_fingerprint": spec.case_fingerprint,
        "source_snapshot_fingerprint": spec.source_snapshot_fingerprint,
        "execution_config_fingerprint": spec.execution_config_fingerprint,
        "variant_id": spec.variant_id.value,
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_scoring_spec_fingerprint(spec: EvalScoringSpec) -> str:
    payload = {
        "schema_version": spec.schema_version,
        "execution_result_fingerprint": spec.execution_result_fingerprint,
        "human_label_fingerprint": spec.human_label_fingerprint,
        "metric_registry_version": spec.metric_registry_version,
        "judge_config_fingerprint": spec.judge_config_fingerprint,
    }
    return _sha256_hex(canonical_json_str(payload))


def compute_variant_output_fingerprint(output: EvalVariantOutput) -> str:
    claims = sorted(
        [
            {
                "claim_id": c.claim_id,
                "statement": c.statement,
                "claim_type": c.claim_type,
                "citation_ids": sorted(c.citation_ids),
            }
            for c in output.claims
        ],
        key=canonical_json_str,
    )
    citations = sorted(
        [
            {
                "citation_id": c.citation_id,
                "source_fingerprint": c.source_fingerprint,
                "locator": c.locator,
                "claim_ids": sorted(c.claim_ids),
            }
            for c in output.citations
        ],
        key=canonical_json_str,
    )
    payload = {
        "schema_version": output.schema_version,
        "variant_id": output.variant_id.value,
        "case_id": output.case_id,
        "case_version": output.case_version,
        "final_text": output.final_text,
        "claims": claims,
        "citations": citations,
        "report_artifact_ref": output.report_artifact_ref,
    }
    return _sha256_hex(canonical_json_str(payload))
