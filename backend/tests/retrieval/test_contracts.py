"""Retrieval contract unit tests (stage 3B.2).

覆盖：RetrievalQuery 校验（company_id / trim / 长度 / top_k / 空 list 归一化 /
timezone-aware / from<=to）、build_chroma_where（chunk_set_id $in、filters 组合
$and、时间 epoch 比较、不支持自定义 where）、RetrievalHit 字段、稳定错误码映射。
不依赖 DB / Chroma / 真实模型。
"""

from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from app.rag.retrieval.contracts import (
    DEFAULT_TOP_K,
    MAX_QUERY_TEXT_CHARS,
    MAX_TOP_K,
    RetrievalHit,
    RetrievalQuery,
    build_chroma_where,
)
from app.rag.retrieval.errors import (
    RetrievalIndexIntegrityError,
    RetrievalIndexNotReady,
    RetrievalOperationFailed,
    RetrievalQueryError,
    stable_error_code,
)

_UTC_1 = datetime(2026, 1, 1, tzinfo=UTC)
_UTC_2 = datetime(2026, 2, 1, tzinfo=UTC)


def _q(**overrides) -> RetrievalQuery:
    base = dict(company_id=uuid4(), query_text="  净利润增长  ")
    base.update(overrides)
    return RetrievalQuery(**base)


class TestRetrievalQuery:
    def test_trims_query_text(self) -> None:
        assert _q().query_text == "净利润增长"

    def test_company_id_required(self) -> None:
        with pytest.raises(RetrievalQueryError):
            RetrievalQuery(company_id=None, query_text="x")
        with pytest.raises(RetrievalQueryError):
            _q(company_id="not-a-uuid")

    def test_blank_query_text_rejected(self) -> None:
        with pytest.raises(RetrievalQueryError):
            _q(query_text="   ")

    def test_query_text_max_length(self) -> None:
        with pytest.raises(RetrievalQueryError):
            _q(query_text="a" * (MAX_QUERY_TEXT_CHARS + 1))
        # 恰好在上限可接受。
        assert _q(query_text="a" * MAX_QUERY_TEXT_CHARS).query_text == "a" * MAX_QUERY_TEXT_CHARS

    def test_top_k_default_and_bounds(self) -> None:
        assert _q().top_k == DEFAULT_TOP_K
        assert _q(top_k=1).top_k == 1
        assert _q(top_k=MAX_TOP_K).top_k == MAX_TOP_K
        for bad in (0, MAX_TOP_K + 1, -5, "5", True):
            with pytest.raises(RetrievalQueryError):
                _q(top_k=bad)

    def test_empty_filter_lists_normalized_to_none(self) -> None:
        query = _q(source_ids=[], provider_keys=[], document_types=[], authority_tiers=[])
        assert query.source_ids is None
        assert query.provider_keys is None
        assert query.document_types is None
        assert query.authority_tiers is None

    def test_naive_datetime_rejected(self) -> None:
        with pytest.raises(RetrievalQueryError):
            _q(published_from=datetime(2026, 1, 1))
        with pytest.raises(RetrievalQueryError):
            _q(published_to=datetime(2026, 1, 1))

    def test_published_range_order(self) -> None:
        with pytest.raises(RetrievalQueryError):
            _q(published_from=_UTC_2, published_to=_UTC_1)
        # from == to 允许。
        assert _q(published_from=_UTC_1, published_to=_UTC_1).published_from == _UTC_1

    def test_reporting_period_range_order(self) -> None:
        with pytest.raises(RetrievalQueryError):
            _q(reporting_period_from=date(2026, 3, 31), reporting_period_to=date(2026, 3, 1))


class TestBuildChromaWhere:
    def _ids(self, count: int = 2) -> list:
        return [uuid4() for _ in range(count)]

    def test_always_has_chunk_set_id_in(self) -> None:
        ids = self._ids()
        where = build_chroma_where(chunk_set_ids=ids, query=_q())
        assert where == {"chunk_set_id": {"$in": [str(cid) for cid in ids]}}

    def test_single_clause_has_no_and(self) -> None:
        where = build_chroma_where(chunk_set_ids=self._ids(1), query=_q())
        assert "$and" not in where

    def test_filters_combine_into_single_and(self) -> None:
        chunk_set_ids = self._ids(1)
        source_ids = self._ids(2)
        query = _q(
            source_ids=source_ids,
            provider_keys=["xinhuanet"],
            document_types=["news_article"],
            authority_tiers=[3, 1],
            critical_claim_eligible_only=True,
        )
        where = build_chroma_where(chunk_set_ids=chunk_set_ids, query=query)
        clauses = where["$and"]
        assert {"chunk_set_id": {"$in": [str(cid) for cid in chunk_set_ids]}} in clauses
        assert {"source_id": {"$in": [str(sid) for sid in source_ids]}} in clauses
        assert {"provider_key": {"$in": ["xinhuanet"]}} in clauses
        assert {"document_type": {"$in": ["news_article"]}} in clauses
        assert {"authority_tier": {"$in": [3, 1]}} in clauses
        assert {"critical_claim_eligible": True} in clauses

    def test_published_time_range_uses_epoch(self) -> None:
        where = build_chroma_where(
            chunk_set_ids=self._ids(1),
            query=_q(published_from=_UTC_1, published_to=_UTC_2),
        )
        clauses = where["$and"]
        assert {"published_at_epoch": {"$gte": int(_UTC_1.timestamp())}} in clauses
        assert {"published_at_epoch": {"$lte": int(_UTC_2.timestamp())}} in clauses

    def test_reporting_period_range_uses_date_epoch(self) -> None:
        from app.rag.retrieval.contracts import date_to_epoch

        where = build_chroma_where(
            chunk_set_ids=self._ids(1),
            query=_q(reporting_period_from=date(2026, 3, 1), reporting_period_to=date(2026, 6, 30)),
        )
        clauses = where["$and"]
        assert {"reporting_period_end_epoch": {"$gte": date_to_epoch(date(2026, 3, 1))}} in clauses
        assert {"reporting_period_end_epoch": {"$lte": date_to_epoch(date(2026, 6, 30))}} in clauses

    def test_no_custom_where_passthrough(self) -> None:
        # 构造 API 不接受任意 where JSON：RetrievalQuery 没有 where 字段。
        assert not hasattr(_q(), "where")


class TestRetrievalHit:
    def test_exposes_distance_not_confidence(self) -> None:
        hit = RetrievalHit(
            rank=1,
            chunk_id=uuid4(),
            chunk_set_id=uuid4(),
            parsed_source_id=uuid4(),
            source_id=uuid4(),
            company_id=uuid4(),
            text="正文",
            distance=0.42,
            provider_key="xinhuanet",
            document_type="news_article",
            source_title="标题",
            source_url="https://example.com",
            published_at=_UTC_1,
            reporting_period_end=date(2026, 6, 30),
            authority_tier=3,
            critical_claim_eligible=False,
            chunk_ordinal=1,
            locator_refs=[],
        )
        assert hit.distance == 0.42
        assert not hasattr(hit, "confidence")
        assert not hasattr(hit, "score")
        assert hit.rank == 1


class TestStableErrorCode:
    def test_retrieval_errors_map_to_own_code(self) -> None:
        assert stable_error_code(RetrievalQueryError()) == "invalid_retrieval_query"
        assert stable_error_code(RetrievalIndexNotReady()) == "retrieval_index_not_ready"
        assert (
            stable_error_code(RetrievalIndexIntegrityError()) == "retrieval_index_integrity_error"
        )
        assert stable_error_code(RetrievalOperationFailed()) == "retrieval_operation_failed"

    def test_unknown_error_falls_back(self) -> None:
        assert stable_error_code(RuntimeError("boom")) == "retrieval_operation_failed"
