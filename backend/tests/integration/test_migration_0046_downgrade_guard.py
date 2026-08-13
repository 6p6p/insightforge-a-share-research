"""Integration test: migration 0046 chunk vector index runtime scope guard.

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0046：

- (B) **backfill + production-only downgrade 通过**：0046 upgrade 为
  `chunk_vector_indexes` 增加 `runtime_scope`；**已存在的 production manifest row**
  （模拟历史行，INSERT 不指定 runtime_scope）被 server default 回填为
  `'production'`。此时全部行 scope='production' → `alembic downgrade 0045`
  成功，列 / 新约束被移除，数据保留（历史 production 行为不变）。
- (A) **eval scope 拒绝**：存在 `runtime_scope != 'production'` 的 manifest row
  （eval 每 attempt 隔离行）→ `alembic downgrade 0045` 必须拒绝（RuntimeError），
  `alembic_version` 仍为 0046，行数据完整保留（不同 attempt 的 manifest 隔离
  语义不能静默丢失）。

全程不触碰主库 `insightforge`，finally 恢复 settings 缓存并 DROP 临时库。
需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
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


async def _seed_chunk_set(temp_url: str) -> str:
    """seed 完整 FK 链到 1 个 ChunkSet，返回 chunk_set_id（manifest FK 需要）。"""
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
            await session.commit()
            return str(chunk_set.chunk_set_id)
    finally:
        await manager.dispose()


async def _insert_manifest_raw_sql(temp_url: str, chunk_set_id: str) -> str:
    """直接 SQL 插入一条 manifest（省略 runtime_scope 列 → server default 回填）。"""
    vector_index_id = str(uuid4())
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO chunk_vector_indexes "
                    "(vector_index_id, chunk_set_id, embedding_model_id, "
                    " embedding_model_revision, embedding_dimension, normalize_embeddings, "
                    " collection_name, collection_schema_version, expected_chunk_count, "
                    " indexed_chunk_count, index_fingerprint, status) "
                    "VALUES (CAST(:vid AS uuid), CAST(:cid AS uuid), 'BAAI/bge-small-zh-v1.5', "
                    " 'test-revision-001', 512, TRUE, 'insightforge_chunks_v2_testfp', 2, "
                    " 1, 1, :fp, 'ready')"
                ).bindparams(
                    vid=vector_index_id,
                    cid=chunk_set_id,
                    fp=_HEX64,
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return vector_index_id


async def _insert_manifest_eval_scope(temp_url: str, chunk_set_id: str) -> str:
    """插入一条 eval scope manifest（runtime_scope != 'production'）。"""
    vector_index_id = str(uuid4())
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO chunk_vector_indexes "
                    "(vector_index_id, chunk_set_id, embedding_model_id, "
                    " embedding_model_revision, runtime_scope, embedding_dimension, "
                    " normalize_embeddings, collection_name, collection_schema_version, "
                    " expected_chunk_count, indexed_chunk_count, index_fingerprint, status) "
                    "VALUES (CAST(:vid AS uuid), CAST(:cid AS uuid), 'BAAI/bge-small-zh-v1.5', "
                    " 'test-revision-001', :scope, 512, TRUE, :coll, 2, 1, 1, :fp, 'ready')"
                ).bindparams(
                    vid=vector_index_id,
                    cid=chunk_set_id,
                    scope="eval:single_rag:abc123",
                    coll="eval_single_rag_abc123",
                    fp=_HEX64,
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return vector_index_id


async def _runtime_scope_of(temp_url: str, vector_index_id: str) -> str | None:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return (
                await session.execute(
                    text(
                        "SELECT runtime_scope FROM chunk_vector_indexes "
                        "WHERE vector_index_id = CAST(:vid AS uuid)"
                    ).bindparams(vid=vector_index_id)
                )
            ).scalar_one_or_none()
    finally:
        await manager.dispose()


async def _scope_column_exists(temp_url: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            exists = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='chunk_vector_indexes' "
                        "AND column_name='runtime_scope'"
                    )
                )
            ).scalar_one()
            return int(exists) == 1
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_0046_upgrade_backfills_production_and_downgrade_when_production_only(
    monkeypatch,
) -> None:
    """(B) 历史 production row 被回填为 'production'；全部 production → 可 downgrade。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0046")
        assert await _version(temp_url) == "0046"
        assert await _scope_column_exists(temp_url) is True

        chunk_set_id = await _seed_chunk_set(temp_url)
        vid = await _insert_manifest_raw_sql(temp_url, chunk_set_id)  # 省略 scope 列
        assert await _runtime_scope_of(temp_url, vid) == "production"  # backfill 生效

        await asyncio.to_thread(command.downgrade, cfg, "0045")
        assert await _version(temp_url) == "0045"
        assert await _scope_column_exists(temp_url) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0046_downgrade_blocked_with_eval_scope(monkeypatch) -> None:
    """(A) eval scope manifest 存在 → 拒绝 downgrade；行数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0046")
        chunk_set_id = await _seed_chunk_set(temp_url)
        vid = await _insert_manifest_eval_scope(temp_url, chunk_set_id)
        assert await _runtime_scope_of(temp_url, vid) == "eval:single_rag:abc123"

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0046"):
            await asyncio.to_thread(command.downgrade, cfg, "0045")

        assert await _version(temp_url) == "0046"
        assert await _scope_column_exists(temp_url) is True
        assert await _runtime_scope_of(temp_url, vid) == "eval:single_rag:abc123"
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
