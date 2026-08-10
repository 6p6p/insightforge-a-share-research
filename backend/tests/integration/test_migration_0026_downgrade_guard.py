"""Integration test: migration 0026 relative valuation downgrade guard (stage 4C.2A, spec Y).

在**独立临时 PostgreSQL 数据库**中真实验证 0026：

- (A) 0026 upgrade 创建三张表（valuation_metric_observations /
  relative_valuation_comparisons / relative_valuation_comparison_peers，含全部
  CHECK / UNIQUE / INDEX）；无数据 → `alembic downgrade 0026 -> 0025` 成功、
  版本回到 0025、三表被删；
- (B) 任一 observation 行存在 → 拒绝 downgrade（拒绝后 alembic_version 仍为
  0026，数据完整保留，不删除 / 不改写 / 不丢弃估值 provenance）；
- (C) observation + comparison + peer links 全部存在 → 拒绝 downgrade，数据完整
  保留（guard 覆盖全部三张表）。

company + metric Evidence 用真实服务链 seed（`_seed_chain`）；valuation 行用
直接 SQL 插入（fingerprint 用生产函数 `compute_valuation_observation_fingerprint` /
`compute_comparison_fingerprint` 生成，满足全部 CHECK）。

测试全程使用 `insightforge_gate_*` 临时库并最终 DROP，不触碰主库
（`insightforge`）。需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
"""

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.models.company import CompanyModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import (
    EvidenceCardDraft,
    EvidenceConfidence,
    EvidenceType,
)
from app.repositories.company_repository import CompanyRepository
from app.repositories.document_chunk_repository import DocumentChunkRepository
from app.repositories.raw_artifact_repository import RawArtifactRepository
from app.repositories.source_provider_repository import SourceProviderRepository
from app.repositories.source_record_repository import SourceRecordRepository
from app.services.chunking_service import ChunkingService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.storage.raw_store import LocalRawArtifactStore
from app.valuation.contracts import (
    compute_comparison_fingerprint,
    compute_valuation_observation_fingerprint,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_METRIC_AS_OF = date(2026, 8, 7)
_ANALYSIS_AS_OF = date(2026, 8, 10)
_PUBLISHED_AT = datetime(2026, 8, 7, 9, 30, tzinfo=UTC)
_HTML = "<p>2024年贵州茅台归属净利润同比增长15.3%。</p>".encode()

_TABLES = (
    "valuation_metric_observations",
    "relative_valuation_comparisons",
    "relative_valuation_comparison_peers",
)


def _parse_db_url(url: str) -> dict:
    parsed = urlparse(url.replace("+psycopg", "", 1))
    return {
        "host": parsed.hostname,
        "port": parsed.port,
        "user": parsed.username,
        "password": parsed.password,
        "dbname": parsed.path.lstrip("/"),
    }


def _admin_conn(db_name: str) -> psycopg.Connection:
    parts = _parse_db_url(get_settings().database_url)
    return psycopg.connect(
        host=parts["host"],
        port=parts["port"],
        user=parts["user"],
        password=parts["password"],
        dbname=db_name,
        autocommit=True,
    )


def _create_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')


def _drop_temp_db(name: str) -> None:
    with _admin_conn("postgres") as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


def _temp_url(base: str, db_name: str) -> str:
    return base.rsplit("/", 1)[0] + f"/{db_name}"


async def _version(temp_url: str) -> str:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return str(
                (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
            )
    finally:
        await manager.dispose()


async def _table_count(temp_url: str, table: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
    finally:
        await manager.dispose()


async def _tables_exist(temp_url: str) -> dict[str, bool]:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name = ANY(:names)"
                        ).bindparams(names=list(_TABLES))
                    )
                )
                .scalars()
                .all()
            )
            return {table: table in set(rows) for table in _TABLES}
    finally:
        await manager.dispose()


async def _seed_chain(temp_url: str, raw_root: Path, code: str) -> dict:
    """company(code) + 真实 document 链 + metric EvidenceCard。

    返回 {company_id, evidence_card_id}（字符串）。
    """
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await SourceProviderRepository(session).upsert(
                SourceProviderModel(
                    provider_key="xinhuanet",
                    display_name="新华网",
                    provider_type="media",
                    authority_tier=3,
                    homepage_url="https://www.xinhuanet.com",
                    allowed_domains=["xinhuanet.com"],
                    capabilities=["news_article"],
                    acquisition_methods=["public_html"],
                    exchange_scope=[],
                    requires_api_key=False,
                    critical_claim_eligible=False,
                    enabled=True,
                )
            )
            await SourceProviderRepository(session).upsert(
                SourceProviderModel(
                    provider_key="sse",
                    display_name="上交所",
                    provider_type="exchange",
                    authority_tier=1,
                    homepage_url="https://www.sse.com.cn",
                    allowed_domains=["sse.com.cn"],
                    capabilities=["company_announcement"],
                    acquisition_methods=["public_html"],
                    exchange_scope=["SSE"],
                    requires_api_key=False,
                    critical_claim_eligible=False,
                    enabled=True,
                )
            )
            company_id = uuid4()
            await CompanyRepository(session).create(
                CompanyModel(
                    company_id=company_id,
                    exchange="SSE",
                    security_code=code,
                    identity_key=f"SSE:{code}",
                    board="sse_main",
                    official_name=f"公司{code}",
                    short_name=code,
                    listing_status="listed",
                    identity_source_provider_key="sse",
                    identity_source_url="https://www.sse.com.cn",
                )
            )
            await session.commit()

        raw_store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
        stored = raw_store.put_html_bytes(_HTML)
        async with sessionmaker() as session:
            artifact = await RawArtifactRepository(session).create(
                RawArtifactModel(
                    content_sha256=stored.content_sha256,
                    storage_key=stored.storage_key,
                    byte_size=stored.byte_size,
                    media_type=stored.media_type,
                )
            )
            if artifact is None:
                artifact = await RawArtifactRepository(session).get_by_sha256(stored.content_sha256)
                assert artifact is not None
            record = SourceRecordModel(
                company_id=company_id,
                provider_key="xinhuanet",
                artifact_id=artifact.artifact_id,
                document_type="news_article",
                title="估值新闻",
                published_at=_PUBLISHED_AT,
                reporting_period_end=None,
                source_url=f"https://www.xinhuanet.com/2026/{code}.htm",
                acquisition_method="public_html",
                status="available",
                authority_tier_snapshot=3,
                critical_claim_eligible_snapshot=False,
                provider_capabilities_snapshot=["news_article"],
                acquired_at=datetime.now(UTC),
            )
            record = await SourceRecordRepository(session).create(record)
            await session.commit()
            source_id = record.source_id

        parsed = await SourceParsingService(sessionmaker, raw_store).parse_source(source_id)
        chunk_result = await ChunkingService(sessionmaker).chunk_parsed_source(
            parsed.parsed_source_id
        )
        async with sessionmaker() as session:
            chunks = await DocumentChunkRepository(session).list_for_chunk_set(
                chunk_result.chunk_set_id
            )
        chunk = chunks[0]
        card = await EvidenceCardService(sessionmaker).create_card(
            EvidenceCardDraft(
                research_question="2024年贵州茅台净利润增长情况？",
                evidence_statement="2024年贵州茅台归属净利润同比增长15%。",
                evidence_type=EvidenceType.METRIC,
                chunk_id=chunk.chunk_id,
                quote_start=0,
                quote_end=20,
                extractor_name="test-extractor",
                extractor_version=1,
                extractor_model_id="test-model",
                extractor_confidence=EvidenceConfidence.HIGH,
            )
        )
        return {
            "company_id": str(company_id),
            "evidence_card_id": str(card.evidence_card_id),
        }
    finally:
        await manager.dispose()


async def _seed_observation(temp_url: str, chain: dict, *, value: str = "15.3") -> dict:
    """直接 SQL 插入一条满足全部 CHECK 的 observation（指纹用生产函数）。"""
    obs_id = uuid4()
    fingerprint = compute_valuation_observation_fingerprint(
        valuation_observation_schema_version=1,
        company_id=UUID(chain["company_id"]),
        source_evidence_card_id=UUID(chain["evidence_card_id"]),
        metric_code="pe_ttm",
        metric_as_of=_METRIC_AS_OF,
        source_value_text=value,
        metric_value=Decimal(value),
    )
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO valuation_metric_observations "
                    "(valuation_observation_id, company_id, source_evidence_card_id, "
                    " metric_code, metric_as_of, source_value_text, metric_value, "
                    " valuation_observation_schema_version, valuation_observation_fingerprint) "
                    "VALUES (CAST(:oid AS uuid), CAST(:company_id AS uuid), "
                    " CAST(:card_id AS uuid), 'pe_ttm', CAST(:asof AS date), "
                    " :value_text, CAST(:value AS numeric), 1, :fp)"
                ).bindparams(
                    oid=obs_id,
                    company_id=UUID(chain["company_id"]),
                    card_id=UUID(chain["evidence_card_id"]),
                    asof=_METRIC_AS_OF,
                    value_text=value,
                    value=Decimal(value),
                    fp=fingerprint,
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return {
        "valuation_observation_id": str(obs_id),
        "valuation_observation_fingerprint": fingerprint,
    }


async def _seed_comparison(temp_url: str, target: dict, peers: list[dict]) -> str:
    """直接 SQL 插入 comparison + 3 peer links（指纹用生产函数，满足全部 CHECK）。"""
    target_company = await _observation_company(temp_url, target["valuation_observation_id"])
    peer_entries = sorted(
        (
            {
                "peer_company_id": peer["company_id"],
                "peer_observation_id": peer["valuation_observation_id"],
                "observation_fingerprint": peer["valuation_observation_fingerprint"],
            }
            for peer in peers
        ),
        key=lambda entry: entry["peer_company_id"],
    )
    peer_values = [Decimal(peer["value"]) for peer in peers]
    peer_median = Decimal("15.0")
    peer_min = min(peer_values)
    peer_max = max(peer_values)
    premium = Decimal("0.02")
    fingerprint = compute_comparison_fingerprint(
        comparison_schema_version=1,
        formula_version=1,
        comparison_method="peer_median",
        target_company_id=UUID(target_company),
        target_observation_id=UUID(target["valuation_observation_id"]),
        target_observation_fingerprint=target["valuation_observation_fingerprint"],
        metric_code="pe_ttm",
        metric_as_of=_METRIC_AS_OF,
        analysis_as_of=_ANALYSIS_AS_OF,
        peers=peer_entries,
        peer_median=peer_median,
        peer_min=peer_min,
        peer_max=peer_max,
        premium_discount_to_median=premium,
    )
    comparison_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO relative_valuation_comparisons "
                    "(comparison_id, target_company_id, target_observation_id, "
                    " metric_code, metric_as_of, analysis_as_of, comparison_method, "
                    " peer_count, peer_median, peer_min, peer_max, "
                    " premium_discount_to_median, comparison_schema_version, "
                    " formula_version, comparison_fingerprint) "
                    "VALUES (CAST(:cid AS uuid), CAST(:target_company AS uuid), "
                    " CAST(:target_obs AS uuid), 'pe_ttm', CAST(:asof AS date), "
                    " CAST(:analysis AS date), 'peer_median', :peer_count, "
                    " CAST(:median AS numeric), CAST(:pmin AS numeric), "
                    " CAST(:pmax AS numeric), CAST(:premium AS numeric), 1, 1, :fp)"
                ).bindparams(
                    cid=comparison_id,
                    target_company=UUID(target_company),
                    target_obs=UUID(target["valuation_observation_id"]),
                    asof=_METRIC_AS_OF,
                    analysis=_ANALYSIS_AS_OF,
                    peer_count=len(peers),
                    median=peer_median,
                    pmin=peer_min,
                    pmax=peer_max,
                    premium=premium,
                    fp=fingerprint,
                )
            )
            for peer in peers:
                await session.execute(
                    text(
                        "INSERT INTO relative_valuation_comparison_peers "
                        "(comparison_id, peer_company_id, peer_observation_id) "
                        "VALUES (CAST(:cid AS uuid), CAST(:peer_company AS uuid), "
                        " CAST(:peer_obs AS uuid))"
                    ).bindparams(
                        cid=comparison_id,
                        peer_company=UUID(peer["company_id"]),
                        peer_obs=UUID(peer["valuation_observation_id"]),
                    )
                )
            await session.commit()
    finally:
        await manager.dispose()
    return str(comparison_id)


async def _observation_company(temp_url: str, observation_id: str) -> str:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            company_id = (
                await session.execute(
                    text(
                        "SELECT company_id FROM valuation_metric_observations "
                        "WHERE valuation_observation_id = :oid"
                    ).bindparams(oid=UUID(observation_id))
                )
            ).scalar_one()
            return str(company_id)
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0026_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(A) 0026 upgrade 建三表；无数据 → downgrade 0026→0025 成功，三表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0026")
        assert await _version(temp_url) == "0026"
        exists = await _tables_exist(temp_url)
        assert all(exists[t] for t in _TABLES)
        assert await _table_count(temp_url, "valuation_metric_observations") == 0
        assert await _table_count(temp_url, "relative_valuation_comparisons") == 0
        assert await _table_count(temp_url, "relative_valuation_comparison_peers") == 0

        await asyncio.to_thread(command.downgrade, cfg, "0025")
        assert await _version(temp_url) == "0025"
        exists = await _tables_exist(temp_url)
        assert all(not exists[t] for t in _TABLES)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0026_downgrade_blocked_with_observation(monkeypatch, tmp_path) -> None:
    """(B) 存在 observation 行 → 拒绝 downgrade，版本保持 0026，数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0026")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        await _seed_observation(temp_url, chain)
        assert await _table_count(temp_url, "valuation_metric_observations") == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0026"):
            await asyncio.to_thread(command.downgrade, cfg, "0025")

        assert await _version(temp_url) == "0026"
        assert await _table_count(temp_url, "valuation_metric_observations") == 1
        assert await _table_count(temp_url, "relative_valuation_comparisons") == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0026_downgrade_blocked_with_comparison_and_peers(
    monkeypatch, tmp_path
) -> None:
    """(C) observation + comparison + peer links 全存在 → 拒绝 downgrade。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0026")
        target_chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        target = await _seed_observation(temp_url, target_chain, value="15.3")
        peers = []
        for i, value in enumerate(["14.2", "15.0", "16.0"]):
            chain = await _seed_chain(temp_url, tmp_path / "raw", f"6005{2 + i:02d}")
            obs = await _seed_observation(temp_url, chain, value=value)
            obs["company_id"] = chain["company_id"]
            obs["value"] = value
            peers.append(obs)
        await _seed_comparison(temp_url, target, peers)

        assert await _table_count(temp_url, "valuation_metric_observations") == 4
        assert await _table_count(temp_url, "relative_valuation_comparisons") == 1
        assert await _table_count(temp_url, "relative_valuation_comparison_peers") == 3

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0026"):
            await asyncio.to_thread(command.downgrade, cfg, "0025")

        assert await _version(temp_url) == "0026"
        assert await _table_count(temp_url, "valuation_metric_observations") == 4
        assert await _table_count(temp_url, "relative_valuation_comparisons") == 1
        assert await _table_count(temp_url, "relative_valuation_comparison_peers") == 3
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
