"""Integration test: migration 0048 company master snapshot downgrade guard.

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_0048_*`）中真实验证 0048：

- 升级到 0048 后 `company_master_snapshots` 表存在（含唯一约束）；
- 表内存在行 → downgrade 拒绝（RuntimeError），版本/数据保留；
- 全部为空 → downgrade 成功。

全程不触碰主库，finally 恢复 settings 缓存并 DROP 临时库。
"""

import asyncio
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager

pytestmark = pytest.mark.integration

configure_asyncio_runtime()

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


def _create_temp_db(db_name: str) -> None:
    params = _parse_db_url(get_settings().database_url)
    params["dbname"] = "postgres"
    with psycopg.connect(**params, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{db_name}"')


def _drop_temp_db(db_name: str) -> None:
    params = _parse_db_url(get_settings().database_url)
    params["dbname"] = "postgres"
    with psycopg.connect(**params, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{db_name}" WITH (FORCE)')


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


async def _table_has_row(temp_url: str, table: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            row = (
                await session.execute(text(f"SELECT 1 FROM {table} LIMIT 1"))
            ).scalar_one_or_none()
            return row is not None
    finally:
        await manager.dispose()


async def _insert_snapshot_row(temp_url: str) -> None:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO company_master_snapshots "
                    "(snapshot_id, snapshot_version, content_sha256, company_count, "
                    "alias_count, sources) VALUES (CAST(:sid AS uuid), :v, :sha, 1, 2, '[]'::jsonb)"
                ).bindparams(sid=str(uuid4()), v="company-master-test-v1", sha="0" * 64)
            )
            await session.commit()
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_0048_upgrade_creates_snapshot_table(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0048_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0048")
        assert await _version(temp_url) == "0048"
        assert await _table_has_row(temp_url, "company_master_snapshots") is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0048_downgrade_blocked_with_rows(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0048_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0048")
        await _insert_snapshot_row(temp_url)
        assert await _table_has_row(temp_url, "company_master_snapshots") is True

        with pytest.raises(RuntimeError, match="refusing silent data loss"):
            await asyncio.to_thread(command.downgrade, cfg, "0047")

        assert await _version(temp_url) == "0048"
        assert await _table_has_row(temp_url, "company_master_snapshots") is True
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0048_downgrade_succeeds_when_empty(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0048_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0048")
        await asyncio.to_thread(command.downgrade, cfg, "0047")
        assert await _version(temp_url) == "0047"
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
