"""Document/event executor tests (stage 7A.2A spec R：document 路径)。

需要真实 PostgreSQL（127.0.0.1:5433）。document/event executor 的
Retrieval 与 extractor 全程注入 **FakeRetrieval + FakeEvidenceExtractionModel**
（0 真实 Retrieval / 0 Chroma / 0 LLM / 0 Web）；source 用真实
`_seed_html_source` 服务链（真实 Parse → Chunk，无 index 写入）。

覆盖（spec J/K/L/P）：
- document need resolved：FakeRetrieval 返回 hit → 抽取 → EvidenceCard；
  第 2 次 fulfill 幂等（fingerprint replay → existing，0 新增写）；
- SOURCE_NOT_FOUND：无匹配 source（区分 "source absent"）；
- INDEX_NOT_READY：source 存在但无 ready index，且无法确定性补建；
  补建失败（IndexBuilder 返回 False）/ 补建成功后再检索 → resolved；
- EVIDENCE_NOT_EXTRACTED：source 有 index 已检索但 0 hits；
- PROVIDER_UNAVAILABLE：route 当时无 provider（不 fetch）；
- event need resolved：event query 模板（question + topic）；
- 防御：need 不在 payload / macro_dataset document need → UNSUPPORTED。
"""

import pytest
import pytest_asyncio

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager
from app.research_fulfillment.contracts import (
    FulfillmentErrorCode,
    FulfillmentStatus,
)
from app.research_fulfillment.executors import DocumentNeedExecutor
from app.research_planning.router import SourceRouteType
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.evidence.fakes import FakeEvidenceExtractionModel
from tests.integration.research_fulfillment_helpers import (
    _decision_for_chunk,
    _FakeIndexBuilder,
    _FakeRetrieval,
    _make_context,
    _make_entry,
    _make_hit,
    _make_need,
)
from tests.integration.test_evidence_card_service import _seed_html_source
from tests.integration.test_research_planning_service import _cleanup
from tests.integration.test_valuation_claim_service import _seed_company

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()


# ---------------------------------------------------------------- env


@pytest_asyncio.fixture
async def database() -> DatabaseManager:
    settings = get_settings()
    manager = DatabaseManager(
        database_url=settings.database_url,
        echo=False,
        connect_timeout_seconds=settings.database_connect_timeout_seconds,
    )
    yield manager
    await manager.dispose()


@pytest_asyncio.fixture
async def sessionmaker(database):
    return database.session_factory()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_store = LocalRawArtifactStore(root=tmp_path / "raw", max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    await SourceRegistryService(sessionmaker).seed_defaults()
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


def _document_executor(
    env: dict,
    *,
    retrieval: _FakeRetrieval | None = None,
    extractor: FakeEvidenceExtractionModel | None = None,
    index_builder: _FakeIndexBuilder | None = None,
) -> DocumentNeedExecutor:
    return DocumentNeedExecutor(
        env["sessionmaker"],
        retrieval_service=retrieval or _FakeRetrieval(),
        extractor_model=extractor or FakeEvidenceExtractionModel(),
        index_builder=index_builder,
    )


async def _card_count(env: dict) -> int:
    async with env["sessionmaker"]() as session:
        from sqlalchemy import text

        return int(
            (await session.execute(text("SELECT count(*) FROM evidence_cards"))).scalar_one()
        )


# ---------------------------------------------------------------- resolved


async def test_document_need_resolved_creates_cards(env) -> None:
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    decision = _decision_for_chunk(chunk)
    extractor = FakeEvidenceExtractionModel(decision=decision)
    retrieval = _FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunk)])
    executor = _document_executor(env, retrieval=retrieval, extractor=extractor)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert attempt.error_code is None
    assert attempt.need_code == "news_docs"
    assert len(attempt.created_artifact_ids) == 1
    assert attempt.existing_artifact_ids == []
    assert await _card_count(env) == 1
    # 确定性 query：question + purpose 模板，source 过滤；extractor 被调用 1 次。
    assert len(retrieval.calls) == 1
    assert retrieval.calls[0].source_ids == [src]
    assert "需要公司新闻" in retrieval.calls[0].query_text
    assert len(extractor.calls) == 1


async def test_document_need_repeated_fulfill_is_idempotent(env) -> None:
    """spec Q：第 2 次 fulfill 同一 missing need → fingerprint replay，0 新增写。"""
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    decision = _decision_for_chunk(chunk)
    executor = _document_executor(
        env,
        retrieval=_FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunk)]),
        extractor=FakeEvidenceExtractionModel(decision=decision),
    )
    ctx = _make_context(env)
    need = _make_need("news_docs")
    entry = _make_entry("news_docs")

    first = await executor.fulfill(context=ctx, need=need, entry=entry)
    second = await executor.fulfill(context=ctx, need=need, entry=entry)

    assert first.status == FulfillmentStatus.RESOLVED
    assert len(first.created_artifact_ids) == 1
    assert second.status == FulfillmentStatus.RESOLVED
    assert second.created_artifact_ids == []
    assert second.existing_artifact_ids == first.created_artifact_ids
    assert await _card_count(env) == 1  # 0 新增写


# ---------------------------------------------------------------- 失败分类


async def test_document_need_no_eligible_source(env) -> None:
    """无匹配 source → SOURCE_NOT_FOUND（source absent）。"""
    retrieval = _FakeRetrieval()
    executor = _document_executor(env, retrieval=retrieval)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.SOURCE_NOT_FOUND
    assert retrieval.calls == []  # 无 source → 不检索


async def test_document_need_index_not_ready(env) -> None:
    """source 存在但无 ready index 且无法确定性补建 → INDEX_NOT_READY。"""
    await _seed_html_source(env)
    retrieval = _FakeRetrieval(not_ready=True)
    executor = _document_executor(env, retrieval=retrieval, index_builder=None)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.INDEX_NOT_READY
    assert len(retrieval.calls) == 1
    assert await _card_count(env) == 0


async def test_document_need_index_builder_fails(env) -> None:
    """补建尝试失败（IndexBuilder 返回 False）→ 仍 INDEX_NOT_READY，不泄漏。"""
    src, _, _, _ = await _seed_html_source(env)
    retrieval = _FakeRetrieval(not_ready=True)
    index_builder = _FakeIndexBuilder(result=False)
    executor = _document_executor(env, retrieval=retrieval, index_builder=index_builder)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.INDEX_NOT_READY
    assert index_builder.calls == [src]
    assert len(retrieval.calls) == 1  # 补建失败 → 不重试检索
    assert await _card_count(env) == 0


async def test_document_need_index_built_then_retry_succeeds(env) -> None:
    """补建成功（IndexBuilder=True）→ 重试检索成功 → resolved。"""
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    decision = _decision_for_chunk(chunk)
    retrieval = _FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunk)], ready_after=True)
    index_builder = _FakeIndexBuilder(result=True)
    executor = _document_executor(
        env,
        retrieval=retrieval,
        extractor=FakeEvidenceExtractionModel(decision=decision),
        index_builder=index_builder,
    )

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert len(attempt.created_artifact_ids) == 1
    assert index_builder.calls == [src]
    assert len(retrieval.calls) == 2  # 首次抛 not-ready，补建后重试
    assert await _card_count(env) == 1


async def test_document_need_no_hits_evidence_not_extracted(env) -> None:
    """source 有 index 且已检索但 0 证据 → EVIDENCE_NOT_EXTRACTED（source 存在）。"""
    _, _, _, chunks = await _seed_html_source(env)
    retrieval = _FakeRetrieval(hits=[])  # 检索返回空
    executor = _document_executor(env, retrieval=retrieval)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.EVIDENCE_NOT_EXTRACTED
    assert len(retrieval.calls) == 1
    assert await _card_count(env) == 0


async def test_document_need_provider_unavailable(env) -> None:
    """route 当时无 provider → PROVIDER_UNAVAILABLE（不检索、不 fetch）。"""
    retrieval = _FakeRetrieval()
    executor = _document_executor(env, retrieval=retrieval)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs", provider_keys=()),
    )

    assert attempt.status == FulfillmentStatus.PROVIDER_UNAVAILABLE
    assert attempt.error_code == FulfillmentErrorCode.PROVIDER_UNAVAILABLE
    assert retrieval.calls == []


# ---------------------------------------------------------------- event / 防御


async def test_event_need_resolved_via_event_query(env) -> None:
    """event need：query = question + topic，document_types=news_article。"""
    src, parsed_id, _, chunks = await _seed_html_source(env)
    chunk = chunks[0]
    decision = _decision_for_chunk(chunk)
    extractor = FakeEvidenceExtractionModel(decision=decision)
    retrieval = _FakeRetrieval(hits=[_make_hit(env, src, parsed_id, chunk)])
    executor = _document_executor(env, retrieval=retrieval, extractor=extractor)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("events", need_kind="event"),
        entry=_make_entry("events", need_kind="event"),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert len(attempt.created_artifact_ids) == 1
    assert retrieval.calls[0].document_types == ["news_article"]
    assert "公司事件" in retrieval.calls[0].query_text
    assert await _card_count(env) == 1


async def test_document_need_not_in_payload_unsupported(env) -> None:
    """need 不在 payload.document_needs → UNSUPPORTED（防御分支）。"""
    from tests.integration.test_research_planning_service import _plan_payload

    executor = _document_executor(env)
    context = _make_context(
        env,
        payload=_plan_payload(
            document_needs=[], event_needs=[], macro_needs=[], valuation_needs=[]
        ),
    )

    attempt = await executor.fulfill(
        context=context,
        need=_make_need("news_docs"),
        entry=_make_entry("news_docs"),
    )

    assert attempt.status == FulfillmentStatus.UNSUPPORTED
    assert attempt.error_code == FulfillmentErrorCode.UNSUPPORTED_NEED


async def test_macro_dataset_document_need_unsupported_in_document_executor(env) -> None:
    """macro_dataset document need 由 service 路由到 macro executor；document
    executor 保留 UNSUPPORTED 防御（不误处理宏观数据形态）。"""
    from tests.integration.test_research_planning_service import _plan_payload

    executor = _document_executor(env)
    context = _make_context(
        env,
        payload=_plan_payload(
            document_needs=[
                {
                    "need_code": "macro_docs",
                    "purpose": "需要宏观数据",
                    "source_type": "macro_dataset",
                }
            ],
            event_needs=[],
            macro_needs=[],
            valuation_needs=[],
        ),
    )

    attempt = await executor.fulfill(
        context=context,
        need=_make_need("macro_docs"),
        entry=_make_entry(
            "macro_docs",
            route_type=SourceRouteType.MACRO_DATA,
            provider_keys=("world_bank",),
        ),
    )

    assert attempt.status == FulfillmentStatus.UNSUPPORTED
    assert attempt.error_code == FulfillmentErrorCode.UNSUPPORTED_NEED
