"""GDELT DOC 2.0 artlist JSON parser (stage 2D.1).

把已通过 JSON 合法性校验的 payload（dict / list / 标量）转换为
NewsDiscoveryCandidate 元组。解析策略是"宽容单条、严格结构"：

- 顶层必须是 object；articles 缺失视为空结果，articles 类型错误拒绝整个 payload；
- 单条 article 非 object / 缺 url / 缺 title / URL 非 http/https / seen date
  无法解析 → 跳过该条，不让整个查询失败，绝不使用当前时间替代缺失时间；
- domain 不盲信 Provider 字段，一律由 normalized_url 的 hostname 派生；
- source_language / source_country 只做 trim + 空→None；
- rank 在过滤后从 1 重新编号；同一 normalized_url 只保留第一条（稳定去重）。
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import (
    NewsDiscoveryCandidate,
    _validate_candidate_url,
    normalize_discovery_url,
)
from app.news.errors import NewsDiscoveryInvalidQuery
from app.news.gdelt.errors import GdeltMalformedResponse

_SEEN_DATE_FORMATS = (
    "%Y%m%d%H%M%S",
    "%Y-%m-%dT%H:%M:%S",
)


@dataclass(frozen=True)
class GdeltDocParser:
    """纯函数式解析器：无状态、不访问网络、不写日志。"""

    @staticmethod
    def parse(payload: object) -> tuple[NewsDiscoveryCandidate, ...]:
        if not isinstance(payload, dict):
            raise GdeltMalformedResponse()
        articles = payload.get("articles")
        if articles is None:
            return ()
        if not isinstance(articles, list):
            raise GdeltMalformedResponse()

        seen_normalized: set[str] = set()
        candidates: list[NewsDiscoveryCandidate] = []
        for raw in articles:
            if not isinstance(raw, dict):
                # 单条非 object：跳过，不让整个查询失败。
                continue
            title = raw.get("title")
            url = raw.get("url")
            if not isinstance(title, str) or not title.strip():
                continue
            if not isinstance(url, str) or not url.strip():
                continue
            discovered = url.strip()
            try:
                _validate_candidate_url(discovered)
            except NewsDiscoveryInvalidQuery:
                # URL 非 http/https / 含 userinfo / hostname 非法：跳过。
                continue
            normalized = normalize_discovery_url(discovered)
            if normalized in seen_normalized:
                continue
            seen_at = _parse_seen_datetime(raw.get("seendate"))
            if seen_at is None:
                # seen date 无法解析：跳过，不替代当前时间。
                continue
            seen_normalized.add(normalized)
            hostname = urlsplit(normalized).hostname or ""
            candidates.append(
                NewsDiscoveryCandidate(
                    rank=len(candidates) + 1,
                    title=title.strip(),
                    discovered_url=discovered,
                    seen_at=seen_at,
                    engine=NewsDiscoveryEngine.GDELT_DOC,
                    source_language=_clean_optional(raw.get("language")),
                    source_country=_clean_optional(raw.get("sourcecountry")),
                    normalized_url=normalized,
                    domain=hostname,
                )
            )
        return tuple(candidates)


def _parse_seen_datetime(value: object) -> datetime | None:
    """解析 GDELT seendate（UTC）。无法解析返回 None，绝不代之以当前时间。"""
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    for fmt in _SEEN_DATE_FORMATS:
        try:
            parsed = datetime.strptime(candidate, fmt)
        except ValueError:
            continue
        return parsed.replace(tzinfo=UTC)
    return None


def _clean_optional(value: object) -> str | None:
    """Provider 附带的 language / sourcecountry：trim，空字符串转 None。"""
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None
