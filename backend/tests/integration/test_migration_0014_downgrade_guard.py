"""Integration test: migration 0014 downgrade guard (stage 3A Gate).

在**独立临时 PostgreSQL 数据库**中真实验证 0014 的 downgrade guard：
当 `chunk_sets` 已有数据时，`alembic downgrade 0014 -> 0013` 必须被
0014 downgrade 的数据安全防护拒绝（RuntimeError），且：

- `alembic_version` 仍为 0014（版本未被回退）；
- `chunk_sets` / `document_chunks` 数据完整保留。

测试全程使用 `insightforge_gate_*` 临时库并最终 DROP，不触碰主库
（`insightforge`），也不改动共享的 DatabaseManager 设置（finally 恢复）。
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
from app.db.models.document_chunk import DocumentChunkModel
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


async def _seed_chunk_set(temp_url: str) -> tuple:
    """在临时库中 seed 完整 FK 链 + 1 个 ChunkSet（含 1 个 Chunk）。

    返回 (provider_key, company_id, chunk_set_id)。

    这些模型之间未定义 SQLAlchemy `relationship()`，UoW 无法对跨 mapper
    的 INSERT 按 FK 依赖排序（只能按 mapper 注册顺序），因此按依赖层逐步
    `flush()`（与项目 repository 逐个 commit 的模式一致），避免依赖方先于
    被依赖方落库触发 FK violation。
    """
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        provider_key = "xinhuanet"
        async with sessionmaker() as session:
            session.add(
                SourceProviderModel(
                    provider_key=provider_key,
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
                provider_key=provider_key,
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

            session.add(
                DocumentChunkModel(
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
            )
            await session.commit()
            return provider_key, company.company_id, chunk_set.chunk_set_id
    finally:
        await manager.dispose()


async def _counts(temp_url: str) -> tuple[int, int]:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            sets = (await session.execute(text("SELECT count(*) FROM chunk_sets"))).scalar_one()
            chunks = (
                await session.execute(text("SELECT count(*) FROM document_chunks"))
            ).scalar_one()
            version = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one()
            return int(sets), int(chunks), str(version)
    finally:
        await manager.dispose()


async def _run_alembic_downgrade(cfg: Config, revision: str) -> None:
    await asyncio.to_thread(command.downgrade, cfg, revision)


@pytest.mark.asyncio
async def test_migration_0014_downgrade_guard_blocks_with_data(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        # 把临时库升级到 head（当前 = 0014）。
        await asyncio.to_thread(command.upgrade, cfg, "0014")

        # seed 完整 FK 链 + 1 个 ChunkSet。
        await _seed_chunk_set(temp_url)
        sets, chunks, version = await _counts(temp_url)
        assert (sets, chunks) == (1, 1)
        assert version == "0014"

        # 有 ChunkSet 数据时 downgrade 必须被拒绝。
        with pytest.raises(RuntimeError, match="chunk_sets contains rows"):
            await _run_alembic_downgrade(cfg, "0013")

        # guard 拒绝后：版本仍为 0014、数据完整保留。
        sets, chunks, version = await _counts(temp_url)
        assert (sets, chunks) == (1, 1)
        assert version == "0014"
    finally:
        # _drop_temp_db 内部会调用 get_settings() 重建缓存（此时 DATABASE_URL
        # 仍指向临时库），因此必须先 drop 再清缓存，避免临时 URL 泄漏到后续测试。
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0014_downgrade_guard_allows_when_empty(monkeypatch) -> None:
    """chunk_sets 无数据时 downgrade 可正常完成（空库防护路径）。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0014")

        # 空库：downgrade 0013 应成功，0014 的两张表被删除。
        await asyncio.to_thread(command.downgrade, cfg, "0013")
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                version = (
                    await session.execute(text("SELECT version_num FROM alembic_version"))
                ).scalar_one()
                assert version == "0013"
                result = await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name IN "
                        "('chunk_sets','document_chunks')"
                    )
                )
                assert result.scalar_one() == 0
        finally:
            await manager.dispose()
    finally:
        # 同第一个测试：先 drop 再清缓存，避免临时 URL 泄漏到后续测试。
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
