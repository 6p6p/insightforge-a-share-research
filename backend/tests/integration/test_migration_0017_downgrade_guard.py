"""Integration test: migration 0017 downgrade guard (stage 3C.3A).

在**独立临时 PostgreSQL 数据库**中真实验证 0017：

- (A) 旧 document v1 行迁移后仍可读：0016 seed document EvidenceCard
  （origin_type 列尚不存在）→ upgrade 0017 → origin_type 回填
  'document_chunk'、其余字段原样保留 → 无 macro 行时 downgrade 0017→0016
  成功，document 行完整保留（不静默丢失 origin semantics）。
- (B) 存在 macro_observation origin 行时 downgrade 0017→0016 必须被拒绝
  （RuntimeError），且 alembic_version 仍为 0017、macro 卡数据完整保留。

测试全程使用 `insightforge_gate_*` 临时库并最终 DROP，不触碰主库
（`insightforge`）。需要真实 PostgreSQL（127.0.0.1:5433）且账号有
CREATEDB 权限。
"""

import asyncio
import hashlib
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import httpx
import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.models.chunk_set import ChunkSetModel
from app.db.models.company import CompanyModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager
from app.evidence.contracts import EvidenceConfidence, MacroEvidenceDraft
from app.macro.world_bank.client import REQUEST_LIMIT, WorldBankClient
from app.macro.world_bank.provider import WorldBankProvider
from app.repositories.company_repository import CompanyRepository
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

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_SHA = hashlib.sha256("测试正文".encode()).hexdigest()
_HEX64 = "a" * 64

_REAL_CLIENT_INIT = WorldBankClient.__init__

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


async def _seed_document_chain_v1(temp_url: str) -> None:
    """在 0016 schema 下 seed 完整 document FK 链 + 1 张 v1 EvidenceCard。

    evidence_cards 用 **raw SQL** 插入（0016 无 origin_type / macro_* 列，
    不能用当前 EvidenceCardModel）。
    """
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            session.add(
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
            session.add(
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
            await session.flush()
            company = CompanyModel(
                company_id=uuid4(),
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
            session.add(company)
            await session.flush()
            artifact = RawArtifactModel(
                artifact_id=uuid4(),
                content_sha256=_SHA,
                storage_key=f"sha256/{_SHA[:2]}/{_SHA[2:4]}/{_SHA}.html",
                byte_size=100,
                media_type="text/html",
            )
            session.add(artifact)
            await session.flush()
            record = SourceRecordModel(
                source_id=uuid4(),
                company_id=company.company_id,
                provider_key="xinhuanet",
                artifact_id=artifact.artifact_id,
                document_type="news_article",
                title="新闻标题",
                published_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
                source_url="https://www.xinhuanet.com/2026/0807/0001.htm",
                acquisition_method="public_html",
                authority_tier_snapshot=3,
                critical_claim_eligible_snapshot=False,
                provider_capabilities_snapshot=["news_article"],
                status="available",
                acquired_at=datetime.now(UTC),
            )
            session.add(record)
            await session.flush()
            parsed = ParsedSourceModel(
                parsed_source_id=uuid4(),
                source_id=record.source_id,
                artifact_id=artifact.artifact_id,
                parser_name="html_dom",
                parser_version=2,
                raw_content_sha256=_SHA,
                parse_fingerprint=_HEX64,
                extracted_title="新闻标题",
                extracted_published_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
                block_count=1,
                parsed_at=datetime.now(UTC),
            )
            session.add(parsed)
            await session.flush()
            chunk_set = ChunkSetModel(
                chunk_set_id=uuid4(),
                parsed_source_id=parsed.parsed_source_id,
                chunker_name="block_window",
                chunker_version=1,
                source_parse_fingerprint=_HEX64,
                chunk_count=1,
                chunk_set_fingerprint=_HEX64,
            )
            session.add(chunk_set)
            await session.flush()
            chunk = DocumentChunkModel(
                chunk_id=uuid4(),
                chunk_set_id=chunk_set.chunk_set_id,
                ordinal=1,
                text="测试正文",
                text_sha256=_SHA,
                char_count=4,
                locator_refs=[
                    {
                        "block_ordinal": 1,
                        "char_start": 0,
                        "char_end": 4,
                        "locator": {
                            "type": "html_dom",
                            "ordinal": 1,
                            "tag": "p",
                            "xpath": "/html/body/article/p[1]",
                            "element_id": None,
                        },
                    }
                ],
            )
            session.add(chunk)
            await session.flush()

            await session.execute(
                text(
                    "INSERT INTO evidence_cards ("
                    "evidence_card_id, company_id, source_id, parsed_source_id, "
                    "chunk_set_id, chunk_id, research_question, research_question_sha256, "
                    "evidence_statement, evidence_type, quote_start, quote_end, quote_text, "
                    "quote_sha256, locator_refs, provider_key, source_published_at, "
                    "reporting_period_end, authority_tier_snapshot, "
                    "critical_claim_eligible_snapshot, extractor_name, extractor_version, "
                    "extractor_model_id, extractor_confidence, evidence_schema_version, "
                    "evidence_fingerprint) VALUES ("
                    ":evidence_card_id, :company_id, :source_id, :parsed_source_id, "
                    ":chunk_set_id, :chunk_id, :research_question, :research_question_sha256, "
                    ":evidence_statement, 'fact', 0, 4, :quote_text, :quote_sha256, "
                    "CAST(:locator_refs AS jsonb), 'xinhuanet', :published_at, NULL, 3, "
                    "false, 'test-extractor', 1, NULL, 'high', 1, :fingerprint)"
                ),
                {
                    "evidence_card_id": str(uuid4()),
                    "company_id": str(company.company_id),
                    "source_id": str(record.source_id),
                    "parsed_source_id": str(parsed.parsed_source_id),
                    "chunk_set_id": str(chunk_set.chunk_set_id),
                    "chunk_id": str(chunk.chunk_id),
                    "research_question": "测试研究问题",
                    "research_question_sha256": hashlib.sha256("测试研究问题".encode()).hexdigest(),
                    "evidence_statement": "测试证据陈述",
                    "quote_text": "测试正文",
                    "quote_sha256": _SHA,
                    "locator_refs": '[{"block_ordinal":1,"char_start":0,"char_end":4,'
                    '"locator":{"type":"html_dom","ordinal":1,"tag":"p",'
                    '"xpath":"/html/body/article/p[1]","element_id":null}}]',
                    "published_at": datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
                    "fingerprint": _HEX64,
                },
            )
            await session.commit()
    finally:
        await manager.dispose()


def _router(request: httpx.Request) -> httpx.Response:
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


async def _seed_macro_card(temp_url: str, raw_root: Path) -> str:
    """在 0017 schema 下 seed world_bank 链 + 用 MacroEvidenceService 建 macro 卡。

    返回 evidence_card_id（字符串）。
    """
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
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
                transport=httpx.MockTransport(_router),
                timeout=timeout,
                request_limit=request_limit,
            )

        original_init = WorldBankClient.__init__
        WorldBankClient.__init__ = _patched_init  # type: ignore[method-assign]
        try:
            provider = WorldBankProvider(sessionmaker)
            captured = await provider.fetch_with_capture(QUERY)
        finally:
            WorldBankClient.__init__ = original_init  # type: ignore[method-assign]

        raw_store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
        result = await MacroPersistenceService(sessionmaker, raw_store).persist_captured_fetch(
            captured
        )
        async with sessionmaker() as session:
            from app.repositories.macro_observation_repository import (
                MacroObservationRepository,
            )

            observations = await MacroObservationRepository(session).list_for_snapshot(
                result.snapshot_id
            )
        obs_2024 = next(o for o in observations if o.period == "2024")
        draft = MacroEvidenceDraft(
            company_id=company_id,
            research_question="中国2024年人口规模？",
            macro_observation_id=obs_2024.observation_id,
            evidence_statement="2024年中国总人口为14.10000004亿人。",
            extractor_name="macro-extractor",
            extractor_version=1,
            extractor_model_id="deepseek:deepseek-v4-flash",
            extractor_confidence=EvidenceConfidence.HIGH,
        )
        card_result = await MacroEvidenceService(sessionmaker).create_macro_card(draft)
        return str(card_result.evidence_card_id)
    finally:
        await manager.dispose()


async def _read_document_row(temp_url: str, *, include_origin: bool = False) -> dict:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            cols = [
                "evidence_statement",
                "source_id",
                "chunk_id",
                "quote_text",
                "quote_sha256",
                "evidence_schema_version",
                "evidence_fingerprint",
            ]
            if include_origin:
                cols.append("origin_type")
            select_sql = "SELECT " + ", ".join(cols) + " FROM evidence_cards"
            row = (await session.execute(text(select_sql))).mappings().one()
            return dict(row)
    finally:
        await manager.dispose()


async def _counts(temp_url: str) -> tuple[int, str]:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            evidence = (
                await session.execute(text("SELECT count(*) FROM evidence_cards"))
            ).scalar_one()
            version = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            return int(evidence), str(version)
    finally:
        await manager.dispose()


async def _run_alembic_downgrade(cfg: Config, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, cfg, revision)


@pytest.mark.asyncio
async def test_migration_0017_backfills_and_allows_downgrade_without_macro(monkeypatch) -> None:
    """(A) 旧 v1 document 行 → 0017 回填 document_chunk 且仍可读；无 macro 行可降级。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0016")
        await _seed_document_chain_v1(temp_url)

        # 0016 时该行无 origin_type 列（v1）。
        row_v1 = await _read_document_row(temp_url)
        assert "origin_type" not in row_v1

        # 升级 0017：origin_type 回填 document_chunk，其余字段原样保留。
        await asyncio.to_thread(command.upgrade, cfg, "0017")
        row = await _read_document_row(temp_url, include_origin=True)
        assert row["origin_type"] == "document_chunk"
        assert row["evidence_statement"] == "测试证据陈述"
        assert row["quote_text"] == "测试正文"
        assert row["quote_sha256"] == _SHA
        assert row["evidence_schema_version"] == 1  # 旧 fingerprint 不重算
        assert row["evidence_fingerprint"] == _HEX64
        assert row["source_id"] is not None
        assert row["chunk_id"] is not None

        # 无 macro 行：downgrade 0017→0016 成功，document 行完整保留。
        await _run_alembic_downgrade(cfg, "0016")
        evidence, version = await _counts(temp_url)
        assert version == "0016"
        assert evidence == 1
        row_after = await _read_document_row(temp_url)
        assert row_after["evidence_statement"] == "测试证据陈述"
        assert row_after["quote_text"] == "测试正文"
        assert "origin_type" not in row_after
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0017_downgrade_blocks_with_macro_cards(monkeypatch, tmp_path) -> None:
    """(B) 存在 macro_observation origin 行时 downgrade 0017→0016 必须被拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0017")
        card_id = await _seed_macro_card(temp_url, tmp_path / "raw")

        with pytest.raises(RuntimeError, match="macro_observation evidence cards present"):
            await _run_alembic_downgrade(cfg, "0016")

        # guard 拒绝后：版本仍为 0017、macro 卡数据完整保留。
        evidence, version = await _counts(temp_url)
        assert evidence == 1
        assert version == "0017"
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                origin = (
                    await session.execute(
                        text("SELECT origin_type FROM evidence_cards WHERE evidence_card_id = :id"),
                        {"id": card_id},
                    )
                ).scalar_one()
                assert origin == "macro_observation"
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
