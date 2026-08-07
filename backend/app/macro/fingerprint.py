"""Snapshot fingerprint v1 (stage 2C.2B).

build_macro_snapshot_fingerprint 对一次获取的领域结果 + 各原始响应的归档
descriptor 构造 canonical JSON 并返回 64 位小写 SHA-256。

稳定化规则：
- 排除 fetched_at / created_at / request_count / snapshot_id / series_id /
  artifact_id / storage_key —— 否则重复获取无法 replay 到同一 fingerprint；
- capabilities 稳定排序（MacroFetchResult 已排序）；
- topics 按 (topic_id, name) 稳定排序；
- observations 按 (normalized_period_start, period) 稳定排序；
- Decimal 用 str(value) 确定性字符串，并保留 decimal_scale（1.0 与 1.00
  规范上可区分）；
- raw_responses 按 role（indicator_metadata → country_metadata →
  observations_page）再 page ASC 排序；
- content_type 使用规范化基础类型 application/json（DB Artifact Link 可保存
  实际响应 header）；
- null 保持 JSON null；
- 结果与输入顺序无关。

fingerprint 算法变化时必须升级 fingerprint_version（当前 v1），不得随手修改
golden 测试值。
"""

import hashlib
import json
from dataclasses import dataclass

from app.domain.macro_persistence import MacroSnapshotArtifactRole
from app.macro.contracts import MacroFetchResult, MacroFrequency

MACRO_SNAPSHOT_FINGERPRINT_VERSION = 1
WORLD_BANK_NORMALIZATION_VERSION = "world_bank_v1"
_FINGERPRINT_CONTENT_TYPE = "application/json"

_ROLE_ORDER = {
    MacroSnapshotArtifactRole.INDICATOR_METADATA: 0,
    MacroSnapshotArtifactRole.COUNTRY_METADATA: 1,
    MacroSnapshotArtifactRole.OBSERVATIONS_PAGE: 2,
}


@dataclass(frozen=True)
class FingerprintArtifact:
    """一次原始响应归档后的 fingerprint 输入（不含 storage_key/id）。"""

    role: MacroSnapshotArtifactRole
    page: int | None
    sha256: str
    response_status: int
    final_hostname: str
    content_type: str


def _canonical_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_macro_snapshot_fingerprint(
    result: MacroFetchResult,
    stored_responses: tuple[FingerprintArtifact, ...],
) -> str:
    raw_responses = [
        {
            "role": artifact.role.value,
            "page": artifact.page,
            "sha256": artifact.sha256,
            "response_status": artifact.response_status,
            "final_hostname": artifact.final_hostname,
            "content_type": _FINGERPRINT_CONTENT_TYPE,
        }
        for artifact in sorted(
            stored_responses,
            key=lambda a: (_ROLE_ORDER[a.role], a.page if a.page is not None else -1),
        )
    ]
    payload = {
        "fingerprint_version": MACRO_SNAPSHOT_FINGERPRINT_VERSION,
        "normalization_version": WORLD_BANK_NORMALIZATION_VERSION,
        "series": {
            "provider_key": result.provider_key,
            "source_id": result.source_id,
            "external_indicator_id": result.indicator.external_indicator_id,
            "geography_type": result.geography.geography_type.value,
            "geography_code": result.geography.iso3_code,
            "frequency": MacroFrequency.ANNUAL.value,
        },
        "query": {
            "requested_country_code": result.query.country_code,
            "start_year": result.query.start_year,
            "end_year": result.query.end_year,
        },
        "provider_snapshot": {
            "acquisition_method": result.acquisition_method.value,
            "authority_tier": int(result.authority_tier),
            "critical_claim_eligible": result.critical_claim_eligible,
            "capabilities": [cap.value for cap in result.provider_capabilities],
        },
        "indicator": {
            "name": result.indicator.name,
            "unit": result.indicator.unit,
            "source_id": result.indicator.source_id,
            "source_name": result.indicator.source_name,
            "source_note": result.indicator.source_note,
            "source_organization": result.indicator.source_organization,
            "topics": sorted(
                (
                    {"topic_id": topic.topic_id, "name": topic.name}
                    for topic in result.indicator.topics
                ),
                key=lambda topic: (topic["topic_id"], topic["name"]),
            ),
        },
        "geography": {
            "requested_code": result.geography.requested_code,
            "provider_country_id": result.geography.provider_country_id,
            "iso2_code": result.geography.iso2_code,
            "iso3_code": result.geography.iso3_code,
            "name": result.geography.name,
            "region_name": result.geography.region_name,
            "income_level_name": result.geography.income_level_name,
        },
        "page_info": {
            "page": result.page_info.page,
            "pages": result.page_info.pages,
            "per_page": result.page_info.per_page,
            "total": result.page_info.total,
            "last_updated": result.page_info.last_updated,
        },
        "observations": [
            {
                "period": observation.period,
                "normalized_period_start": observation.normalized_period_start.isoformat(),
                "frequency": observation.frequency.value,
                "value": (None if observation.value is None else str(observation.value)),
                "is_missing": observation.is_missing,
                "decimal_scale": observation.decimal_scale,
                "observation_status": observation.observation_status,
            }
            for observation in sorted(
                result.observations,
                key=lambda o: (o.normalized_period_start, o.period),
            )
        ],
        "raw_responses": raw_responses,
    }
    canonical = _canonical_dumps(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
