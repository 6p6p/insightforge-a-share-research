"""Discovery query fingerprint v1 (stage 2D.1).

build_query_fingerprint 对"一次发现查询 + 其归档原始响应的内容寻址"构造
canonical JSON 并返回 64 位小写 SHA-256。

canonical JSON 只包含：
- engine
- company_id
- query_text
- start_at（UTC ISO）
- end_at（UTC ISO）
- max_results
- raw_content_sha256

明确不包含 fetched_at / request_count / 任何 ID / storage_key —— 否则完全
相同的 discovery response 无法 replay 到同一 Run。

query_fingerprint 不是新闻事实 fingerprint，它只标识"发现过程 + 原始响应"
的确定性版本；"原始新闻是否已验证"由 2D.2 决定。
"""

import hashlib
import json
from datetime import UTC

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryQuery


def _canonical_dumps(payload: object) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def build_query_fingerprint(
    engine: NewsDiscoveryEngine,
    query: NewsDiscoveryQuery,
    raw_content_sha256: str,
) -> str:
    payload = {
        "engine": engine.value,
        "company_id": str(query.company_id),
        "query_text": query.query_text,
        "start_at": query.start_at.astimezone(UTC).isoformat(),
        "end_at": query.end_at.astimezone(UTC).isoformat(),
        "max_results": query.max_results,
        "raw_content_sha256": raw_content_sha256,
    }
    canonical = _canonical_dumps(payload)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_url_sha256(normalized_url: str) -> str:
    """Candidate 的 normalized_url 内容寻址（用于 url_sha256 列）。"""
    return hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()
