"""Integration test: migration 0016 downgrade guard (stage 3C.1).

在**独立临时 PostgreSQL 数据库**中真实验证 0016 的 downgrade guard：

- (A) 0016 + `evidence_cards` 有数据 → `alembic downgrade 0016 -> 0015`
  必须被 0016 downgrade 的数据安全防护拒绝（RuntimeError），且：
  - `alembic_version` 仍为 0016（版本未被回退）；
  - EvidenceCard 数据完整保留。
- (B) 0016 无 evidence card（其余 FK 链数据存在）→ downgrade 0015 成功，
  `evidence_cards` 表被删除，其余链数据保留。

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

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.models.chunk_set import ChunkSetModel
from app.db.models.company import CompanyModel
from app.db.models.document_chunk import DocumentChunkModel
from app.db.models.evidence_card import EvidenceCardModel
from app.db.models.parsed_source import ParsedSourceModel
from app.db.models.raw_artifact import RawArtifactModel
from app.db.models.source_provider import SourceProviderModel
from app.db.models.source_record import SourceRecordModel
from app.db.session import DatabaseManager

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_SHA = hashlib.sha256("测试正文".encode()).hexdigest()
_HEX64 = "a" * 64


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


async def _seed_full_chain(temp_url: str, *, seed_evidence: bool) -> int:
    """在临时库中 seed 完整 FK 链 + 1 个 ChunkSet + 1 个 Chunk。

    seed_evidence=True 时额外插入 1 条 evidence_cards（fact）。
    返回 evidence_cards 数。按依赖层逐步 flush()（模型间无 relationship()，
    UoW 无法按 FK 依赖自动排序）。
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

            if seed_evidence:
                session.add(
                    EvidenceCardModel(
                        evidence_card_id=uuid4(),
                        company_id=company.company_id,
                        source_id=record.source_id,
                        parsed_source_id=parsed.parsed_source_id,
                        chunk_set_id=chunk_set.chunk_set_id,
                        chunk_id=chunk.chunk_id,
                        research_question="测试研究问题",
                        research_question_sha256=hashlib.sha256(
                            "测试研究问题".encode()
                        ).hexdigest(),
                        evidence_statement="测试证据陈述",
                        evidence_type="fact",
                        quote_start=0,
                        quote_end=4,
                        quote_text="测试正文",
                        quote_sha256=_SHA,
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
                        provider_key="xinhuanet",
                        source_published_at=record.published_at,
                        reporting_period_end=record.reporting_period_end,
                        authority_tier_snapshot=3,
                        critical_claim_eligible_snapshot=False,
                        extractor_name="test-extractor",
                        extractor_version=1,
                        extractor_model_id=None,
                        extractor_confidence="high",
                        evidence_schema_version=1,
                        evidence_fingerprint=_HEX64,
                    )
                )
            await session.commit()

            async with sessionmaker() as session2:
                evidence_count = int(
                    (
                        await session2.execute(text("SELECT count(*) FROM evidence_cards"))
                    ).scalar_one()
                )
            return evidence_count
    finally:
        await manager.dispose()


async def _counts(temp_url: str) -> tuple[int, int, int, str]:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            sets = (await session.execute(text("SELECT count(*) FROM chunk_sets"))).scalar_one()
            chunks = (
                await session.execute(text("SELECT count(*) FROM document_chunks"))
            ).scalar_one()
            evidence = (
                await session.execute(text("SELECT count(*) FROM evidence_cards"))
            ).scalar_one()
            version = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            return int(sets), int(chunks), int(evidence), str(version)
    finally:
        await manager.dispose()


async def _run_alembic_downgrade(cfg: Config, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, cfg, revision)


@pytest.mark.asyncio
async def test_migration_0016_downgrade_blocks_with_evidence_data(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        # 临时库升级到 head（当前 = 0016）。
        await asyncio.to_thread(command.upgrade, cfg, "0016")

        # seed 完整 FK 链 + 1 张 EvidenceCard。
        evidence_count = await _seed_full_chain(temp_url, seed_evidence=True)
        assert evidence_count == 1
        sets, chunks, evidence, version = await _counts(temp_url)
        assert (sets, chunks, evidence) == (1, 1, 1)
        assert version == "0016"

        # 有 EvidenceCard 数据时 downgrade 必须被拒绝。
        with pytest.raises(RuntimeError, match="evidence_cards contains rows"):
            await _run_alembic_downgrade(cfg, "0015")

        # guard 拒绝后：版本仍为 0016、EvidenceCard 数据完整保留。
        sets, chunks, evidence, version = await _counts(temp_url)
        assert (sets, chunks, evidence) == (1, 1, 1)
        assert version == "0016"
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0016_downgrade_allows_without_evidence(monkeypatch) -> None:
    """0016 无 EvidenceCard → downgrade 0015 可正常完成（数据安全防护不误伤）。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0016")

        # seed 完整 FK 链，但不插入 EvidenceCard。
        evidence_count = await _seed_full_chain(temp_url, seed_evidence=False)
        assert evidence_count == 0

        # 无 EvidenceCard：downgrade 0015 应成功。
        await _run_alembic_downgrade(cfg, "0015")
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                version = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                assert version == "0015"

                # evidence_cards 表被删除。
                table_count = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name='evidence_cards'"
                        )
                    )
                ).scalar_one()
                assert table_count == 0

                # chunk_sets / document_chunks 数据保留。
                sets = (await session.execute(text("SELECT count(*) FROM chunk_sets"))).scalar_one()
                chunks = (
                    await session.execute(text("SELECT count(*) FROM document_chunks"))
                ).scalar_one()
                assert (int(sets), int(chunks)) == (1, 1)
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
