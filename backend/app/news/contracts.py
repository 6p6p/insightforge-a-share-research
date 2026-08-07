"""News discovery contracts (stage 2D.1).

NewsDiscoveryQuery 描述"发送给 Discovery Provider 的搜索表达式"与时间窗；
NewsDiscoveryCandidate 描述一条发现线索。两者都不是 SourceRecord，也不产生
Evidence。query_text 的准确语义是"显式搜索表达式"：本阶段不自动翻译公司名、
不生成英文别名、不使用 LLM 改写。
"""

import codecs
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.errors import NewsDiscoveryInvalidQuery

_MAX_QUERY_TEXT_LENGTH = 300
_DEFAULT_MAX_RESULTS = 50
_MAX_RESULTS_LIMIT = 100
_MAX_WINDOW_DAYS = 365
_FUTURE_TOLERANCE = timedelta(minutes=5)
_MAX_TITLE_LENGTH = 1000
_DEFAULT_PORTS = {"http": 80, "https": 443}

_CRLF_NUL = "\r\n\x00"

_QUERY_ERROR_MSG = {
    "query_text_type": "query_text 必须是字符串",
    "query_text_blank": "query_text trim 后不能为空",
    "query_text_too_long": f"query_text 最长 {_MAX_QUERY_TEXT_LENGTH} 字符",
    "query_text_ctrl": "query_text 不允许 CR/LF/NUL",
    "datetime_type": "start_at/end_at 必须是 timezone-aware datetime",
    "window_order": "start_at 必须不晚于 end_at",
    "window_span": f"时间窗最长 {_MAX_WINDOW_DAYS} 天",
    "window_future": "end_at 不能晚于当前时间 5 分钟以上",
    "max_results_type": "max_results 必须是 int",
    "max_results_range": f"max_results 必须在 1—{_MAX_RESULTS_LIMIT} 之间",
}

_CANDIDATE_ERROR_MSG = {
    "rank_type": "rank 必须是 int",
    "rank_range": "rank 必须 >= 1",
    "title_type": "title 必须是字符串",
    "title_blank": "title trim 后不能为空",
    "title_too_long": f"title 最长 {_MAX_TITLE_LENGTH} 字符",
    "url_type": "URL 必须是字符串",
    "url_scheme": "URL 只允许 http/https",
    "url_userinfo": "URL 不允许 userinfo",
    "url_hostname": "URL hostname 必须合法",
    "normalized_mismatch": "normalized_url 必须等于对 discovered_url 的确定性 normalization",
    "domain_mismatch": "domain 必须等于 normalized_url 的 hostname",
    "seen_at": "seen_at 必须是 timezone-aware datetime",
    "engine": "engine 必须是 NewsDiscoveryEngine",
}


def normalize_discovery_url(url: str) -> str:
    """对发现候选 URL 做确定性 normalization。

    - scheme lowercase；hostname IDNA + lowercase；
    - 删除 scheme 对应的默认端口（http→80、https→443）；
    - 保留 path 与 query；删除 fragment；
    - 不删除 utm 参数、不重排 query、不猜 canonical URL、不 follow redirect；
    - 拒绝 http/https 之外的 scheme、userinfo、空/非法 hostname。
    """
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise NewsDiscoveryInvalidQuery("URL 只允许 http/https")
    if parsed.username is not None or parsed.password is not None:
        raise NewsDiscoveryInvalidQuery("URL 不允许 userinfo")
    hostname = parsed.hostname
    if not hostname:
        raise NewsDiscoveryInvalidQuery("URL hostname 必须合法")
    try:
        ascii_host = codecs.encode(hostname, "idna").decode("ascii")
    except UnicodeError:
        raise NewsDiscoveryInvalidQuery("URL hostname 必须合法") from None
    try:
        port = parsed.port
    except ValueError:
        raise NewsDiscoveryInvalidQuery("URL hostname 必须合法") from None
    default_port = _DEFAULT_PORTS[parsed.scheme]
    netloc = ascii_host if port in (None, default_port) else f"{ascii_host}:{port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, ""))


def _is_aware(dt: datetime) -> bool:
    return dt.tzinfo is not None and dt.utcoffset() is not None


@dataclass(frozen=True)
class NewsDiscoveryQuery:
    """一次新闻发现查询（发送给 Discovery Provider 的搜索表达式 + 时间窗）。"""

    company_id: UUID
    query_text: str
    start_at: datetime
    end_at: datetime
    max_results: int = _DEFAULT_MAX_RESULTS

    def __post_init__(self) -> None:
        if not isinstance(self.query_text, str):
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["query_text_type"])
        if any(c in self.query_text for c in _CRLF_NUL):
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["query_text_ctrl"])
        stripped = self.query_text.strip()
        if not stripped:
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["query_text_blank"])
        if len(stripped) > _MAX_QUERY_TEXT_LENGTH:
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["query_text_too_long"])
        object.__setattr__(self, "query_text", stripped)
        for name in ("start_at", "end_at"):
            value = getattr(self, name)
            if not isinstance(value, datetime) or not _is_aware(value):
                raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["datetime_type"])
        if self.start_at > self.end_at:
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["window_order"])
        if self.end_at - self.start_at > timedelta(days=_MAX_WINDOW_DAYS):
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["window_span"])
        if self.end_at.astimezone(UTC) > datetime.now(UTC) + _FUTURE_TOLERANCE:
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["window_future"])
        if isinstance(self.max_results, bool) or not isinstance(self.max_results, int):
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["max_results_type"])
        if not 1 <= self.max_results <= _MAX_RESULTS_LIMIT:
            raise NewsDiscoveryInvalidQuery(_QUERY_ERROR_MSG["max_results_range"])


@dataclass(frozen=True)
class NewsDiscoveryCandidate:
    """一条新闻发现候选（只是线索，不是 SourceRecord、不是 Evidence）。

    - discovered_url：保留 Provider 返回 URL 的原始语义（trim 后保存），
      本阶段允许 http（Candidate 尚未被网络访问）；
    - normalized_url：对 discovered_url 的确定性 normalization（见
      normalize_discovery_url）；不删除 utm 参数、不重排 query；
    - domain：必须等于 normalized_url 的 hostname（不盲信 Provider 字段）。
    """

    rank: int
    title: str
    discovered_url: str
    seen_at: datetime
    engine: NewsDiscoveryEngine
    source_language: str | None = None
    source_country: str | None = None
    normalized_url: str | None = None
    domain: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.rank, bool) or not isinstance(self.rank, int):
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["rank_type"])
        if self.rank < 1:
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["rank_range"])
        if not isinstance(self.title, str):
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["title_type"])
        stripped_title = self.title.strip()
        if not stripped_title:
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["title_blank"])
        if len(stripped_title) > _MAX_TITLE_LENGTH:
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["title_too_long"])
        object.__setattr__(self, "title", stripped_title)
        if not isinstance(self.discovered_url, str) or not self.discovered_url.strip():
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["url_type"])
        discovered = self.discovered_url.strip()
        object.__setattr__(self, "discovered_url", discovered)
        # discovered_url 本身也必须满足 http/https、无 userinfo、hostname 合法。
        _validate_candidate_url(discovered)
        normalized = normalize_discovery_url(discovered)
        if self.normalized_url is not None:
            if self.normalized_url != normalized:
                raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["normalized_mismatch"])
        else:
            object.__setattr__(self, "normalized_url", normalized)
        hostname = urlsplit(normalized).hostname or ""
        if self.domain is not None:
            if self.domain != hostname:
                raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["domain_mismatch"])
        else:
            object.__setattr__(self, "domain", hostname)
        if not isinstance(self.seen_at, datetime) or not _is_aware(self.seen_at):
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["seen_at"])
        if not isinstance(self.engine, NewsDiscoveryEngine):
            raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["engine"])
        for name in ("source_language", "source_country"):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, str):
                    raise NewsDiscoveryInvalidQuery(f"{name} 必须是字符串或 None")
                if not value.strip():
                    object.__setattr__(self, name, None)


def _validate_candidate_url(url: str) -> None:
    """URL 只允许 http/https、无 userinfo、hostname 合法（候选本身也要满足）。"""
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https"):
        raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["url_scheme"])
    if parsed.username is not None or parsed.password is not None:
        raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["url_userinfo"])
    hostname = parsed.hostname
    if not hostname:
        raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["url_hostname"])
    try:
        codecs.encode(hostname, "idna")
    except UnicodeError:
        raise NewsDiscoveryInvalidQuery(_CANDIDATE_ERROR_MSG["url_hostname"]) from None
