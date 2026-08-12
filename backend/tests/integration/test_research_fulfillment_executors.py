"""Financial / macro / valuation executor tests (stage 7A.2A spec R)。

需要真实 PostgreSQL（127.0.0.1:5433）。这三个 executor 都是确定性
（0 LLM / 0 Retrieval / 0 Chroma / 0 Web）：financial 从既有
FinancialMetricObservation 推导 create_calculation；macro 从既有
MacroObservation / Snapshot / Series 确定性匹配后 replay macro Evidence
（seed 用 MockTransport，不访问真实 World Bank）；valuation 恒返回
manual_required。

覆盖（spec M/N/O）：
- financial resolved：revenue 观察齐备 → create_calculation → RESOLVED；
  第 2 次 fulfill 幂等（fingerprint replay → existing，0 新增写）；
- financial MISSING_UNDERLYING_OBSERVATION：底层观测缺失（不凭空造数）；
- financial PROVIDER_UNAVAILABLE：route 无 provider；
- macro resolved：可用观测 + topic/geo 匹配 → create_macro_card → RESOLVED；
- macro MACRO_DATA_UNAVAILABLE：topic 不匹配（不 live fetch）；
- macro PROVIDER_UNAVAILABLE：route 无 provider；
- valuation manual_required + EXPLICIT_PEER_SET_REQUIRED（不自动 peer）。
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
from app.research_fulfillment.executors import (
    FinancialNeedExecutor,
    MacroNeedExecutor,
    ValuationNeedExecutor,
)
from app.research_planning.router import SourceRouteType
from app.services.source_registry_service import SourceRegistryService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.research_fulfillment_helpers import (
    _make_context,
    _make_entry,
    _make_need,
    _seed_evidence_card,
    _seed_revenue_pair,
    _seed_world_bank_provider,
)
from tests.integration.test_macro_evidence_service import _seed_macro_chain
from tests.integration.test_research_planning_service import _cleanup, _plan_payload
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
    await _seed_world_bank_provider(sessionmaker)
    company_id = await _seed_company(sessionmaker, "600519")
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": raw_store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


async def _calc_count(env: dict) -> int:
    from sqlalchemy import text

    async with env["sessionmaker"]() as session:
        return int(
            (
                await session.execute(text("SELECT count(*) FROM financial_calculations"))
            ).scalar_one()
        )


async def _macro_card_count(env: dict) -> int:
    from sqlalchemy import text

    async with env["sessionmaker"]() as session:
        return int(
            (
                await session.execute(
                    text(
                        "SELECT count(*) FROM evidence_cards "
                        "WHERE origin_type = 'macro_observation'"
                    )
                )
            ).scalar_one()
        )


# ---------------------------------------------------------------- financial


def _financial_context(env):
    return _make_context(env)  # _plan_payload 默认含 revenue_change need


def _financial_entry():
    return _make_entry(
        "revenue_change",
        need_kind="financial",
        route_type=SourceRouteType.COMPANY_ANNOUNCEMENT,
        provider_keys=("sse",),
    )


async def test_financial_need_resolved_creates_calculation(env) -> None:
    await _seed_evidence_card(env)
    await _seed_revenue_pair(env)
    executor = FinancialNeedExecutor(env["sessionmaker"])

    attempt = await executor.fulfill(
        context=_financial_context(env),
        need=_make_need("revenue_change", need_kind="financial"),
        entry=_financial_entry(),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert attempt.error_code is None
    assert len(attempt.created_artifact_ids) == 1
    assert attempt.existing_artifact_ids == []
    assert await _calc_count(env) == 1


async def test_financial_need_repeated_fulfill_is_idempotent(env) -> None:
    """spec Q：第 2 次 fulfill 同一 financial need → create_calculation replay。"""
    await _seed_evidence_card(env)
    await _seed_revenue_pair(env)
    executor = FinancialNeedExecutor(env["sessionmaker"])
    ctx = _financial_context(env)
    need = _make_need("revenue_change", need_kind="financial")
    entry = _financial_entry()

    first = await executor.fulfill(context=ctx, need=need, entry=entry)
    second = await executor.fulfill(context=ctx, need=need, entry=entry)

    assert first.status == FulfillmentStatus.RESOLVED
    assert len(first.created_artifact_ids) == 1
    assert second.status == FulfillmentStatus.RESOLVED
    assert second.created_artifact_ids == []
    assert second.existing_artifact_ids == first.created_artifact_ids
    assert await _calc_count(env) == 1  # 0 新增写


async def test_financial_need_missing_underlying_observation(env) -> None:
    """底层观测缺失 → MISSING_UNDERLYING_OBSERVATION（不凭空造数）。"""
    await _seed_evidence_card(env)  # 只 seed 源卡，无 observation
    executor = FinancialNeedExecutor(env["sessionmaker"])

    attempt = await executor.fulfill(
        context=_financial_context(env),
        need=_make_need("revenue_change", need_kind="financial"),
        entry=_financial_entry(),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.MISSING_UNDERLYING_OBSERVATION
    assert await _calc_count(env) == 0


async def test_financial_need_provider_unavailable(env) -> None:
    executor = FinancialNeedExecutor(env["sessionmaker"])

    attempt = await executor.fulfill(
        context=_financial_context(env),
        need=_make_need("revenue_change", need_kind="financial"),
        entry=_make_entry(
            "revenue_change",
            need_kind="financial",
            route_type=SourceRouteType.COMPANY_ANNOUNCEMENT,
            provider_keys=(),
        ),
    )

    assert attempt.status == FulfillmentStatus.PROVIDER_UNAVAILABLE
    assert attempt.error_code == FulfillmentErrorCode.PROVIDER_UNAVAILABLE
    assert await _calc_count(env) == 0


# ---------------------------------------------------------------- macro


def _macro_context(env):
    return _make_context(
        env,
        payload=_plan_payload(
            macro_needs=[
                {
                    "need_code": "macro_pop",
                    "purpose": "需要宏观人口数据",
                    "topic_or_indicator": "Population",
                    "geography": "CHN",
                }
            ]
        ),
    )


def _macro_entry():
    return _make_entry(
        "macro_pop",
        need_kind="macro",
        route_type=SourceRouteType.MACRO_DATA,
        provider_keys=("world_bank",),
    )


async def test_macro_need_resolved_creates_macro_cards(env, monkeypatch) -> None:
    await _seed_macro_chain(env, monkeypatch)
    executor = MacroNeedExecutor(env["sessionmaker"])

    attempt = await executor.fulfill(
        context=_macro_context(env),
        need=_make_need("macro_pop", need_kind="macro"),
        entry=_macro_entry(),
    )

    assert attempt.status == FulfillmentStatus.RESOLVED
    assert attempt.error_code is None
    assert attempt.created_artifact_ids  # 最多 _MAX_MACRO_CARDS 张 macro 卡
    assert len(attempt.created_artifact_ids) <= 5
    assert await _macro_card_count(env) == len(attempt.created_artifact_ids)


async def test_macro_need_repeated_fulfill_is_idempotent(env, monkeypatch) -> None:
    """spec Q：第 2 次 fulfill 同一 macro need → create_macro_card replay。"""
    await _seed_macro_chain(env, monkeypatch)
    executor = MacroNeedExecutor(env["sessionmaker"])
    ctx = _macro_context(env)
    need = _make_need("macro_pop", need_kind="macro")
    entry = _macro_entry()

    first = await executor.fulfill(context=ctx, need=need, entry=entry)
    second = await executor.fulfill(context=ctx, need=need, entry=entry)

    assert first.status == FulfillmentStatus.RESOLVED
    assert second.status == FulfillmentStatus.RESOLVED
    assert second.created_artifact_ids == []
    assert set(second.existing_artifact_ids) == set(first.created_artifact_ids)
    assert await _macro_card_count(env) == len(first.created_artifact_ids)  # 0 新增写


async def test_macro_need_no_matching_data(env, monkeypatch) -> None:
    """topic 不匹配任何可用观测 → MACRO_DATA_UNAVAILABLE（不 live fetch）。"""
    await _seed_macro_chain(env, monkeypatch)
    executor = MacroNeedExecutor(env["sessionmaker"])
    context = _make_context(
        env,
        payload=_plan_payload(
            macro_needs=[
                {
                    "need_code": "macro_gdp",
                    "purpose": "需要宏观数据",
                    "topic_or_indicator": "GDP",
                    "geography": "CHN",
                }
            ]
        ),
    )

    attempt = await executor.fulfill(
        context=context,
        need=_make_need("macro_gdp", need_kind="macro"),
        entry=_macro_entry(),
    )

    assert attempt.status == FulfillmentStatus.UNRESOLVED
    assert attempt.error_code == FulfillmentErrorCode.MACRO_DATA_UNAVAILABLE
    assert await _macro_card_count(env) == 0


async def test_macro_need_provider_unavailable(env, monkeypatch) -> None:
    await _seed_macro_chain(env, monkeypatch)
    executor = MacroNeedExecutor(env["sessionmaker"])

    attempt = await executor.fulfill(
        context=_macro_context(env),
        need=_make_need("macro_pop", need_kind="macro"),
        entry=_make_entry(
            "macro_pop",
            need_kind="macro",
            route_type=SourceRouteType.MACRO_DATA,
            provider_keys=(),
        ),
    )

    assert attempt.status == FulfillmentStatus.PROVIDER_UNAVAILABLE
    assert attempt.error_code == FulfillmentErrorCode.PROVIDER_UNAVAILABLE
    assert await _macro_card_count(env) == 0


# ---------------------------------------------------------------- valuation


async def test_valuation_need_manual_required(env) -> None:
    """valuation need 不自动 peer → manual_required + explicit_peer_set_required。"""
    executor = ValuationNeedExecutor()

    attempt = await executor.fulfill(
        context=_make_context(env),
        need=_make_need("pe_valuation", need_kind="valuation"),
        entry=_make_entry(
            "pe_valuation",
            need_kind="valuation",
            route_type=SourceRouteType.COMPANY_ANNOUNCEMENT,
            provider_keys=("sse",),
        ),
    )

    assert attempt.status == FulfillmentStatus.MANUAL_REQUIRED
    assert attempt.error_code == FulfillmentErrorCode.EXPLICIT_PEER_SET_REQUIRED
    assert attempt.created_artifact_ids == []
    assert attempt.existing_artifact_ids == []
