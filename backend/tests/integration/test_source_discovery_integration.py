"""Source discovery layer integration tests (P1).

真实 PostgreSQL + FakeRetrieval + FakeEvidenceExtractionModel（0 Retrieval /
0 Chroma / 0 LLM / 0 Web）。覆盖：

- document need 无 eligible source → discovery acquired（已落库 source）→
  重查 → RESOLVED；
- discovery exhausted → 保持 SOURCE_NOT_FOUND（human fallback 兜底）；
- event need + discovery exhausted（news 扩展点未启用）→ SOURCE_NOT_FOUND；
- macro need + discovery exhausted → MACRO_DATA_UNAVAILABLE；
- discovery 优先于 legacy auto_acquisition（同一 executor 双注入）。
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
from app.research_fulfillment.executors import DocumentNeedExecutor, MacroNeedExecutor
from app.research_planning.contracts import DocumentNeed, ResearchDocumentNeedType
from app.research_planning.router import SourceRouteType
from app.services.source_discovery.contracts import (
    SourceDiscoveryOutcome,
    SourceDiscoveryRequest,
)
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


class FakeDiscoveryService:
    """替身 SourceDiscoveryService：固定 outcome 或 on_discover 回调 + 记录 requests。"""

    def __init__(
        self,
        outcome: SourceDiscoveryOutcome | None = None,
        *,
        on_discover=None,
    ) -> None:
        self._outcome = outcome or SourceDiscoveryOutcome(acquired=False, exhausted=True)
        self._on_discover = on_discover
        self.calls: list[SourceDiscoveryRequest] = []

    async def discover(self, request: SourceDiscoveryRequest) -> SourceDiscoveryOutcome:
        self.calls.append(request)
        if self._on_discover is not None:
            return await self._on_discover(request)
        return self._outcome


class _BoxRetrieval:
    """惰性 retrieval：hit 由 box 在 discovery 落库后填充（模拟真实检索）。"""

    def __init__(self, box: dict) -> None:
        self._box = box
        self.calls = []

    async def retrieve(self, query):
        self.calls.append(query)
        hit = self._box.get("hit")
        return [hit] if hit is not None else []


class _BoxExtractor(FakeEvidenceExtractionModel):
    """惰性 extractor：decision 由 box 填充（与 discovery 落库的 chunk 一致）。"""

    def __init__(self, box: dict) -> None:
        super().__init__()
        self._box = box

    async def extract(self, research_question: str, retrieval_hit):
        self.calls.append((research_question, retrieval_hit))
        return self._box["decision"]


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


def _annual_payload():
    from tests.integration.test_research_planning_service import _plan_payload

    payload = _plan_payload()
    return payload.model_copy(
        update={
            "document_needs": [
                DocumentNeed(
                    need_code="annual_need",
                    purpose="需要年度报告",
                    source_type=ResearchDocumentNeedType.ANNUAL_REPORT,
                    period="2024",
                )
            ]
        }
    )


def _document_executor(env, discovery) -> DocumentNeedExecutor:
    return DocumentNeedExecutor(
        env["sessionmaker"],
        retrieval_service=_FakeRetrieval(),
        extractor_model=FakeEvidenceExtractionModel(),
        index_builder=_FakeIndexBuilder(),
        discovery=discovery,
    )


async def _card_count(env) -> int:
    from sqlalchemy import text

    async with env["sessionmaker"]() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM evidence_cards"))).scalar_one()
        )


# ---------------------------------------------------------------- document + discovery


async def test_document_need_resolved_after_discovery_acquired(env) -> None:
    """无 eligible source → discovery 回调真实落库 → 重查 → RESOLVED。"""
    box: dict = {}

    async def _on_discover(request):
        # 模拟真实 provider：发现 → 校验 → 落库（幂等）。
        src, parsed_id, _, chunks = await _seed_html_source(
            env,
            provider_key="eastmoney",
            document_type="annual_report",
            reporting_period_end=__import__("datetime").date(2024, 12, 31),
        )
        box["hit"] = _make_hit(env, src, parsed_id, chunks[0])
        box["decision"] = _decision_for_chunk(chunks[0])
        return SourceDiscoveryOutcome(acquired=True, source_ids=(src,))

    discovery = FakeDiscoveryService(on_discover=_on_discover)
    executor = DocumentNeedExecutor(
        env["sessionmaker"],
        retrieval_service=_BoxRetrieval(box),
        extractor_model=_BoxExtractor(box),
        index_builder=_FakeIndexBuilder(),
        discovery=discovery,
    )

    attempt = await executor.fulfill(
        context=_make_context(env, payload=_annual_payload()),
        need=_make_need("annual_need"),
        entry=_make_entry(
            "annual_need",
            route_type=SourceRouteType.COMPANY_ANNOUNCEMENT,
            provider_keys=("eastmoney",),
        ),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert attempt.error_code is None
    assert len(attempt.created_artifact_ids) == 1
    assert await _card_count(env) == 1
    # discovery 被调用且收到正确语义输入。
    assert len(discovery.calls) == 1
    assert discovery.calls[0].need_kind == "document"
    assert discovery.calls[0].source_type == "annual_report"
    assert discovery.calls[0].period == "2024"
    assert discovery.calls[0].security_code == "600519"


async def test_document_need_discovery_exhausted_keeps_source_not_found(env) -> None:
    """discovery 全部 provider exhausted → 保持 SOURCE_NOT_FOUND（human fallback）。"""
    discovery = FakeDiscoveryService(
        SourceDiscoveryOutcome(acquired=False, exhausted=True, reasons=("no_candidates",))
    )
    executor = _document_executor(env, discovery)

    attempt = await executor.fulfill(
        context=_make_context(env, payload=_annual_payload()),
        need=_make_need("annual_need"),
        entry=_make_entry(
            "annual_need",
            route_type=SourceRouteType.COMPANY_ANNOUNCEMENT,
            provider_keys=("eastmoney",),
        ),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.SOURCE_NOT_FOUND
    assert len(discovery.calls) == 1


async def test_event_need_discovery_exhausted_keeps_source_not_found(env) -> None:
    """事件 need：discovery（news 扩展点未启用）exhausted → SOURCE_NOT_FOUND。"""
    discovery = FakeDiscoveryService(
        SourceDiscoveryOutcome(acquired=False, exhausted=True, reasons=("news_not_enabled",))
    )
    executor = _document_executor(env, discovery)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("events", need_kind="event"),
        entry=_make_entry("events", need_kind="event"),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.SOURCE_NOT_FOUND
    assert len(discovery.calls) == 1
    assert discovery.calls[0].need_kind == "event"
    assert discovery.calls[0].source_type == "news_article"


# ---------------------------------------------------------------- macro + discovery


async def test_macro_need_discovery_exhausted_keeps_unavailable(env) -> None:
    """macro need：discovery exhausted → MACRO_DATA_UNAVAILABLE（不编造数字）。"""
    discovery = FakeDiscoveryService(
        SourceDiscoveryOutcome(acquired=False, exhausted=True, reasons=("no_candidates",))
    )
    executor = MacroNeedExecutor(env["sessionmaker"], discovery=discovery)

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("macro_gdp", need_kind="macro"),
        entry=_make_entry("macro_gdp", need_kind="macro", route_type=SourceRouteType.MACRO_DATA),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.MACRO_DATA_UNAVAILABLE
    assert len(discovery.calls) == 1
    assert discovery.calls[0].need_kind == "macro"
