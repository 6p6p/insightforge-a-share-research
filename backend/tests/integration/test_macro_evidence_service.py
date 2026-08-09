"""MacroEvidenceService integration tests (stage 3C.3A, spec 11).

需要真实 PostgreSQL（127.0.0.1:5433）。macro provenance 走真实
MacroPersistenceService（httpx.MockTransport，**不访问真实 World Bank**），
再以 MacroEvidenceService.create_macro_card 登记为 macro_observation origin
的 EvidenceCard。

覆盖：
- 创建 document-free Evidence：origin_type=macro_observation、macro_*
  ids 正确、document provenance + quote 全 NULL、evidence_type=metric、
  locator_refs 为 structured macro locator、schema v2；
- locator 能回溯到 Observation/Snapshot/Series/Provider；
- provider_key / authority_tier_snapshot / critical_claim_eligible_snapshot
  来自真实 Macro provenance（不硬编码 World Bank tier）；
- replay（同 fingerprint 复用同一卡）/ 并发 → 1；
- statement / extractor version 变化 → 新卡，旧卡保留；
- corrupted provenance → EvidenceProvenanceIntegrityError（不自动修复）；
- corrupted replay → EvidenceCardIntegrityError；
- 不创建 DocumentChunk / ChunkSet / ParsedSource / SourceRecord；
- missing observation（is_missing=true）仍可登记为 Evidence。

不读取 Chroma、不调用 LLM（conftest autouse guard 阻止真实外网）。
"""

import asyncio
from datetime import date
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.models.company import CompanyModel
from app.db.models.source_provider import SourceProviderModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceConfidence, MacroEvidenceDraft
from app.evidence.errors import (
    EvidenceCardIntegrityError,
    EvidenceProvenanceIntegrityError,
)
from app.macro.world_bank.client import REQUEST_LIMIT, WorldBankClient
from app.macro.world_bank.provider import WorldBankProvider
from app.repositories.company_repository import CompanyRepository
from app.repositories.evidence_card_repository import EvidenceCardRepository
from app.repositories.macro_observation_repository import MacroObservationRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.services.macro_evidence_service import MacroEvidenceService
from app.services.macro_persistence_service import MacroPersistenceService
from app.storage.raw_store import LocalRawArtifactStore
from tests.macro.world_bank.helpers import (
    QUERY,
    country_response,
    indicator_response,
    json_response,
    observation_row,
    observations_response,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

configure_asyncio_runtime()

_DEFAULT_PROVIDER_KEYS = (
    "sse",
    "szse",
    "bse",
    "cninfo",
    "csrc",
    "nbs",
    "fred",
    "world_bank",
)

_REAL_CLIENT_INIT = WorldBankClient.__init__

_QUESTION = "中国2024年人口规模是多少？"
_STATEMENT = "2024年中国总人口为14.10000004亿人（世界银行 SP.POP.TOTL）。"


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


async def _cleanup(sessionmaker) -> None:
    async with sessionmaker() as session:
        await session.execute(text("DELETE FROM evidence_cards"))
        await session.execute(text("DELETE FROM macro_observations"))
        await session.execute(text("DELETE FROM macro_snapshot_artifacts"))
        await session.execute(text("DELETE FROM macro_dataset_snapshots"))
        await session.execute(text("DELETE FROM macro_series"))
        await session.execute(text("DELETE FROM source_records"))
        await session.execute(text("DELETE FROM raw_artifacts"))
        await session.execute(text("DELETE FROM company_aliases"))
        await session.execute(text("DELETE FROM companies"))
        placeholders = ",".join(f"'{key}'" for key in _DEFAULT_PROVIDER_KEYS)
        await session.execute(
            text(f"DELETE FROM source_providers WHERE provider_key NOT IN ({placeholders})")
        )
        await session.commit()


@pytest_asyncio.fixture
async def env(tmp_path, sessionmaker) -> dict:
    raw_root = tmp_path / "raw"
    store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
    await _cleanup(sessionmaker)
    async with sessionmaker() as session:
        await SourceProviderRepository(session).upsert(
            SourceProviderModel(
                provider_key="world_bank",
                display_name="World Bank Open Data",
                provider_type="international_organization",
                authority_tier=1,
                homepage_url="https://data.worldbank.org",
                allowed_domains=["worldbank.org"],
                capabilities=["macro_data", "document_download"],
                acquisition_methods=["official_api"],
                exchange_scope=[],
                requires_api_key=False,
                critical_claim_eligible=True,
                enabled=True,
            )
        )
        company_id = uuid4()
        await CompanyRepository(session).create(
            CompanyModel(
                company_id=company_id,
                exchange="SSE",
                security_code="600519",
                identity_key="SSE:600519",
                board="sse_main",
                official_name="测试公司",
                short_name="测试",
                listing_status="listed",
                identity_source_provider_key="sse",
                identity_source_url="https://www.sse.com.cn",
            )
        )
        await session.commit()
    yield {
        "sessionmaker": sessionmaker,
        "raw_store": store,
        "company_id": company_id,
    }
    await _cleanup(sessionmaker)


def _router(request: httpx.Request) -> httpx.Response:
    """确定性 MockTransport 路由：indicator + country + 单页 observations。"""
    path = request.url.path
    if path == "/v2/indicator/SP.POP.TOTL":
        return json_response(indicator_response())
    if path == "/v2/country/CHN":
        return json_response(country_response())
    if "/v2/country/CHN/indicator/" in path:
        rows = [
            observation_row(year, value=1400000000 + (year - 2020))
            for year in range(QUERY.start_year, QUERY.end_year + 1)
        ]
        return json_response(
            observations_response(page=1, pages=1, per_page=1000, total=len(rows), rows=rows)
        )
    raise AssertionError(f"unexpected path {path}")


def _build_provider(
    sessionmaker, transport: httpx.AsyncBaseTransport, monkeypatch
) -> WorldBankProvider:
    def _patched_init(
        self,
        *,
        allowed_domains: list[str],
        timeout: httpx.Timeout | None = None,
        request_limit: int = REQUEST_LIMIT,
    ) -> None:
        _REAL_CLIENT_INIT(
            self,
            allowed_domains=allowed_domains,
            transport=transport,
            timeout=timeout,
            request_limit=request_limit,
        )

    monkeypatch.setattr(WorldBankClient, "__init__", _patched_init)
    return WorldBankProvider(sessionmaker)


async def _seed_macro_chain(env: dict, monkeypatch) -> dict:
    """真实持久化一条 macro 链（series/snapshot/artifacts/observations）。

    返回 {series_id, snapshot_id, observation_id(2024), period="2024"}。
    """
    provider = _build_provider(env["sessionmaker"], httpx.MockTransport(_router), monkeypatch)
    captured = await provider.fetch_with_capture(QUERY)
    persistence = MacroPersistenceService(env["sessionmaker"], env["raw_store"])
    result = await persistence.persist_captured_fetch(captured)
    async with env["sessionmaker"]() as session:
        observations = await MacroObservationRepository(session).list_for_snapshot(
            result.snapshot_id
        )
    obs_2024 = next(o for o in observations if o.period == "2024")
    return {
        "series_id": result.series_id,
        "snapshot_id": result.snapshot_id,
        "observation_id": obs_2024.observation_id,
        "period": obs_2024.period,
    }


def _service(env: dict) -> MacroEvidenceService:
    return MacroEvidenceService(env["sessionmaker"])


def _draft(env: dict, chain: dict, **overrides) -> MacroEvidenceDraft:
    values = dict(
        company_id=env["company_id"],
        research_question=_QUESTION,
        macro_observation_id=chain["observation_id"],
        evidence_statement=_STATEMENT,
        extractor_name="macro-extractor",
        extractor_version=1,
        extractor_model_id="deepseek:deepseek-chat",
        extractor_confidence=EvidenceConfidence.HIGH,
    )
    values.update(overrides)
    return MacroEvidenceDraft(**values)


async def _card_count(sessionmaker) -> int:
    async with sessionmaker() as session:
        return int(
            (await session.execute(text("SELECT count(*) FROM evidence_cards"))).scalar_one()
        )


# ---------------------------------------------------------------- 创建


async def test_create_macro_card_persists_document_free_evidence(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    result = await _service(env).create_macro_card(_draft(env, chain))
    assert result.replayed is False
    assert result.chunk_id is None  # macro 不经过 DocumentChunk
    assert len(result.evidence_fingerprint) == 64

    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
        assert card is not None
        assert card.origin_type == "macro_observation"
        assert card.company_id == env["company_id"]
        assert card.macro_observation_id == chain["observation_id"]
        assert card.macro_snapshot_id == chain["snapshot_id"]
        assert card.macro_series_id == chain["series_id"]
        # document-specific 全部 NULL
        assert card.source_id is None
        assert card.parsed_source_id is None
        assert card.chunk_set_id is None
        assert card.chunk_id is None
        assert card.quote_start is None
        assert card.quote_end is None
        assert card.quote_text is None
        assert card.quote_sha256 is None
        assert card.source_published_at is None
        assert card.reporting_period_end is None
        # 固定语义
        assert card.evidence_type == "metric"
        assert card.evidence_schema_version == 2
        assert card.provider_key == "world_bank"


async def test_macro_locator_traces_to_observation_snapshot_series(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    result = await _service(env).create_macro_card(_draft(env, chain))
    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
    assert card is not None
    assert card.locator_refs
    entry = card.locator_refs[0]
    assert entry["type"] == "macro_observation"
    assert entry["provider_key"] == "world_bank"
    assert entry["series_id"] == str(chain["series_id"])
    assert entry["snapshot_id"] == str(chain["snapshot_id"])
    assert entry["observation_id"] == str(chain["observation_id"])
    assert entry["period"] == "2024"
    assert entry["external_indicator_id"] == "SP.POP.TOTL"
    assert entry["geography_code"] == "CHN"
    assert entry["frequency"] == "annual"
    assert entry["normalized_period_start"] == date(2024, 1, 1).isoformat()


async def test_authority_snapshot_comes_from_provenance_not_hardcoded(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    # 把 snapshot 的获取时快照改成非 World Bank 默认值：卡必须复制该值，
    # 证明 authority tier / critical eligibility 不硬编码。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE macro_dataset_snapshots SET authority_tier_snapshot = 2, "
                "critical_claim_eligible_snapshot = false "
                "WHERE snapshot_id = :sid"
            ),
            {"sid": chain["snapshot_id"]},
        )
        await session.commit()

    result = await _service(env).create_macro_card(_draft(env, chain))
    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
    assert card is not None
    assert card.authority_tier_snapshot == 2
    assert card.critical_claim_eligible_snapshot is False


async def test_create_macro_card_requires_existing_company(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    unknown = uuid4()
    with pytest.raises(EvidenceProvenanceIntegrityError):
        await _service(env).create_macro_card(_draft(env, chain, company_id=unknown))
    assert await _card_count(env["sessionmaker"]) == 0


# ---------------------------------------------------------------- replay / 并发


async def test_replay_returns_same_card(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    service = _service(env)
    draft = _draft(env, chain)
    first = await service.create_macro_card(draft)
    second = await service.create_macro_card(draft)
    assert first.replayed is False
    assert second.replayed is True
    assert first.evidence_card_id == second.evidence_card_id
    assert await _card_count(env["sessionmaker"]) == 1


async def test_concurrent_create_yields_single_card(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    service = _service(env)
    draft = _draft(env, chain)
    results = await asyncio.gather(*(service.create_macro_card(draft) for _ in range(5)))
    ids = {r.evidence_card_id for r in results}
    assert len(ids) == 1
    assert sum(1 for r in results if r.replayed) == 4
    assert sum(1 for r in results if not r.replayed) == 1
    assert await _card_count(env["sessionmaker"]) == 1


async def test_statement_change_creates_new_card(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    service = _service(env)
    a = await service.create_macro_card(_draft(env, chain, evidence_statement="表述A"))
    b = await service.create_macro_card(_draft(env, chain, evidence_statement="表述B"))
    assert a.evidence_card_id != b.evidence_card_id
    assert b.replayed is False
    assert await _card_count(env["sessionmaker"]) == 2  # 旧卡保留


async def test_extractor_version_change_creates_new_card(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    service = _service(env)
    a = await service.create_macro_card(_draft(env, chain, extractor_version=1))
    b = await service.create_macro_card(_draft(env, chain, extractor_version=2))
    assert a.evidence_card_id != b.evidence_card_id
    assert await _card_count(env["sessionmaker"]) == 2


# ---------------------------------------------------------------- integrity


async def test_corrupted_provenance_raises_integrity_error(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    # 删除 snapshot（RESTRICT 保护下需先删依赖；直接 DROP 属损坏场景）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text("DELETE FROM macro_observations WHERE snapshot_id = :sid"),
            {"sid": chain["snapshot_id"]},
        )
        await session.execute(
            text("DELETE FROM macro_snapshot_artifacts WHERE snapshot_id = :sid"),
            {"sid": chain["snapshot_id"]},
        )
        await session.execute(
            text("DELETE FROM macro_dataset_snapshots WHERE snapshot_id = :sid"),
            {"sid": chain["snapshot_id"]},
        )
        await session.commit()

    with pytest.raises(EvidenceProvenanceIntegrityError):
        await _service(env).create_macro_card(_draft(env, chain))
    assert await _card_count(env["sessionmaker"]) == 0


async def test_corrupted_replay_raises_integrity_error(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    service = _service(env)
    first = await service.create_macro_card(_draft(env, chain))
    # 篡改已落库卡的 evidence_statement：replay 时必须逐字段比对失败。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE evidence_cards SET evidence_statement = '篡改' WHERE evidence_card_id = :id"
            ),
            {"id": first.evidence_card_id},
        )
        await session.commit()

    with pytest.raises(EvidenceCardIntegrityError):
        await service.create_macro_card(_draft(env, chain))


# ---------------------------------------------------------------- 边界


async def test_create_macro_card_creates_no_document_rows(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    await _service(env).create_macro_card(_draft(env, chain))
    async with env["sessionmaker"]() as session:
        for table in (
            "document_chunks",
            "chunk_sets",
            "parsed_sources",
            "source_records",
        ):
            count = int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
            assert count == 0, f"macro evidence must not create {table} rows"


async def test_missing_observation_still_registerable(env, monkeypatch) -> None:
    chain = await _seed_macro_chain(env, monkeypatch)
    # 把 2024 观测标记为 missing（CHECK：value_numeric IS NULL 且 is_missing=true
    # 且 decimal_scale IS NULL）。
    async with env["sessionmaker"]() as session:
        await session.execute(
            text(
                "UPDATE macro_observations SET value_numeric = NULL, is_missing = true, "
                "decimal_scale = NULL WHERE observation_id = :oid"
            ),
            {"oid": chain["observation_id"]},
        )
        await session.commit()

    result = await _service(env).create_macro_card(_draft(env, chain))
    assert result.replayed is False
    async with env["sessionmaker"]() as session:
        card = await EvidenceCardRepository(session).get_by_id(result.evidence_card_id)
    assert card is not None
    assert card.origin_type == "macro_observation"
