"""Integration test: migration 0018 downgrade guard (stage 4A).

在**独立临时 PostgreSQL 数据库**中真实验证 0018：

- (A) 0018 创建 claims / claim_evidence_links；无数据 → `alembic
  downgrade 0018 -> 0017` 成功，两张表被删除，版本回到 0017；
- (B) 存在 Claim + ClaimEvidenceLink 数据时 downgrade 0018→0017 必须被
  拒绝（RuntimeError），且 alembic_version 仍为 0018、Claim 与 link 数据
  完整保留（不静默丢弃 Claim 证据链）。

(B) 的 Claim 用**真实服务链** seed：SourceRecord → SourceParsingService →
ChunkingService → EvidenceCardService.create_card → ClaimService.create_claim
（零 Chroma / 零 LLM）。测试全程使用 `insightforge_gate_*` 临时库并最终
DROP，不触碰主库（`insightforge`）。需要真实 PostgreSQL（127.0.0.1:5433）
且账号有 CREATEDB 权限。
"""

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.claims.contracts import (
    ClaimAnalysisDomain,
    ClaimConfidence,
    ClaimDraft,
    ClaimImportance,
    ClaimKind,
)
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
from app.services.claim_service import ClaimService
from app.services.evidence_card_service import EvidenceCardService
from app.services.source_parsing_service import SourceParsingService
from app.storage.raw_store import LocalRawArtifactStore
from tests.integration.test_evidence_card_service import _MULTI_HTML, _SOURCE_TITLE

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"


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


async def _count_tables(temp_url: str, tables: tuple[str, ...]) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            placeholders = ",".join(f"'{t}'" for t in tables)
            return int(
                (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name IN (" + placeholders + ")"
                        )
                    )
                ).scalar_one()
            )
    finally:
        await manager.dispose()


async def _version(temp_url: str) -> str:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            version = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            return str(version)
    finally:
        await manager.dispose()


async def _seed_document_claim(temp_url: str, raw_root: Path) -> dict:
    """在 0018 schema 下用真实服务链 seed 一条 document Claim。

    返回 {claim_id, evidence_card_id}（字符串）。
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

        # 真实 HTML 链：raw artifact → source record → parsed → chunk。
        raw_store = LocalRawArtifactStore(root=raw_root, max_bytes=1024 * 1024)
        stored = raw_store.put_html_bytes(_MULTI_HTML)
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
                title=_SOURCE_TITLE,
                published_at=datetime(2026, 8, 7, 9, 30, tzinfo=UTC),
                source_url="https://www.xinhuanet.com/2026/0809/0001.htm",
                acquisition_method="public_html",
                authority_tier_snapshot=3,
                critical_claim_eligible_snapshot=False,
                provider_capabilities_snapshot=["news_article"],
                status="available",
                acquired_at=datetime.now(UTC),
            )
            record = await SourceRecordRepository(session).create(record)
            await session.commit()
            source_id = record.source_id

        parsed_service = SourceParsingService(sessionmaker, raw_store)
        parsed = await parsed_service.parse_source(source_id)
        chunk_result = await ChunkingService(sessionmaker).chunk_parsed_source(
            parsed.parsed_source_id
        )
        async with sessionmaker() as session:
            chunks = await DocumentChunkRepository(session).list_for_chunk_set(
                chunk_result.chunk_set_id
            )
        chunk = chunks[0]

        # EvidenceCardService → ClaimService（真实持久化）。
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
        claim = await ClaimService(sessionmaker).create_claim(
            ClaimDraft(
                company_id=company_id,
                research_question="2024年贵州茅台净利润增长情况？",
                statement="2024年贵州茅台归属净利润同比增长15%。",
                analysis_domain=ClaimAnalysisDomain.FINANCIAL,
                claim_kind=ClaimKind.FACT,
                confidence=ClaimConfidence.HIGH,
                importance=ClaimImportance.NORMAL,
                support_evidence_ids=[card.evidence_card_id],
                contradict_evidence_ids=[],
                context_evidence_ids=[],
                analyst_name="structured-analyst",
                analyst_version=1,
                analyst_model_id="deepseek:deepseek-v4-flash",
            )
        )
        return {
            "claim_id": str(claim.claim_id),
            "evidence_card_id": str(card.evidence_card_id),
        }
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0018_downgrade_allowed_when_empty(monkeypatch) -> None:
    """(A) 0018 无数据 → downgrade 0018→0017 成功，claims 表被删除。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0018")
        claim_tables = await _count_tables(temp_url, ("claims", "claim_evidence_links"))
        assert claim_tables == 2  # 0018 确实创建了这两张表

        await asyncio.to_thread(command.downgrade, cfg, "0017")
        assert await _version(temp_url) == "0017"
        assert await _count_tables(temp_url, ("claims", "claim_evidence_links")) == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0018_downgrade_blocked_with_claim_data(monkeypatch, tmp_path) -> None:
    """(B) 存在 Claim + Link 数据时 downgrade 0018→0017 必须被拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0018")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")

        with pytest.raises(RuntimeError, match="claims/claim_evidence_links rows present"):
            await asyncio.to_thread(command.downgrade, cfg, "0017")

        # guard 拒绝后：版本仍为 0018、Claim 与 link 数据完整保留。
        assert await _version(temp_url) == "0018"
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                claim_id = UUID(seeded["claim_id"])
                evidence_card_id = UUID(seeded["evidence_card_id"])
                claims = (
                    await session.execute(
                        text("SELECT count(*) FROM claims WHERE claim_id = :cid").bindparams(
                            cid=claim_id
                        )
                    )
                ).scalar_one()
                links = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM claim_evidence_links "
                            "WHERE claim_id = :cid AND evidence_card_id = :eid"
                        ).bindparams(cid=claim_id, eid=evidence_card_id)
                    )
                ).scalar_one()
        finally:
            await manager.dispose()
        assert claims == 1
        assert links == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
