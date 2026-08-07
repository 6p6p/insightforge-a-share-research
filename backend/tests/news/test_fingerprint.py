"""Unit tests for discovery query fingerprint v1 (stage 2D.1).

覆盖 §十八 D：
- golden vector：固定输入 → 固定 SHA-256（canonical JSON 序列化/排序任一
  规则变化都会使测试失败）；
- 确定性：同输入同指纹；raw sha 不同 → 不同；
- 敏感性：query_text / 时间窗 / max_results 任一变化 → 不同；
- fingerprint 只含 engine + company_id + query_text + UTC ISO 时间窗 +
  max_results + raw_content_sha256（不含 fetched_at / request_count / ID）；
- build_url_sha256 内容寻址确定性。
"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID

from app.domain.news_discovery import NewsDiscoveryEngine
from app.news.contracts import NewsDiscoveryQuery
from app.news.fingerprint import build_query_fingerprint, build_url_sha256

# golden vector：由 build_query_fingerprint 对该固定输入产生（canonical JSON
# 排序 / separators / UTC ISO 任一变化都会使此值失效）。
GOLDEN = "63d770607289e2acf3bb33c63ad7584851dd1478a88254a77886fdee838054ac"

_RAW_SHA = "a" * 64
_COMPANY_ID = UUID("11111111-2222-3333-4444-555555555555")


def _query(**overrides: object) -> NewsDiscoveryQuery:
    values: dict = {
        "company_id": _COMPANY_ID,
        "query_text": "Kweichow Moutai",
        "start_at": datetime(2026, 8, 1, tzinfo=UTC),
        "end_at": datetime(2026, 8, 7, tzinfo=UTC),
        "max_results": 10,
    }
    values.update(overrides)
    return NewsDiscoveryQuery(**values)


def _fp(query: NewsDiscoveryQuery, raw_sha: str = _RAW_SHA) -> str:
    return build_query_fingerprint(NewsDiscoveryEngine.GDELT_DOC, query, raw_sha)


def test_golden_vector() -> None:
    assert _fp(_query()) == GOLDEN


def test_same_input_same_fingerprint() -> None:
    assert _fp(_query()) == _fp(_query())


def test_raw_sha_differs_fingerprint_differs() -> None:
    assert _fp(_query(), raw_sha="b" * 64) != GOLDEN


def test_query_text_differs_fingerprint_differs() -> None:
    assert _fp(_query(query_text="Moutai")) != GOLDEN


def test_window_differs_fingerprint_differs() -> None:
    later = datetime(2026, 8, 5, tzinfo=UTC)
    assert _fp(_query(end_at=later)) != GOLDEN


def test_max_results_differs_fingerprint_differs() -> None:
    assert _fp(_query(max_results=50)) != GOLDEN


def test_company_differs_fingerprint_differs() -> None:
    other = UUID("22222222-2222-3333-4444-555555555555")
    assert _fp(_query(company_id=other)) != GOLDEN


def test_timezone_normalized_to_utc() -> None:
    # 同一时刻的不同时区表示应产生相同指纹（astimezone(UTC) 归一化）。
    q_tz = _query(start_at=datetime(2026, 8, 1, 8, 0, tzinfo=UTC))
    q_utc8 = _query(start_at=datetime(2026, 8, 1, 16, 0, tzinfo=timezone(timedelta(hours=8))))
    assert _fp(q_tz) == _fp(q_utc8)


def test_url_sha256_content_addressed() -> None:
    assert len(build_url_sha256("https://example.com/a")) == 64
    assert build_url_sha256("https://example.com/a") == build_url_sha256("https://example.com/a")
    assert build_url_sha256("https://example.com/a") != build_url_sha256("https://example.com/b")
