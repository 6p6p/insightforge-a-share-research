"""Integration test: migration 0052 backflow human review closure downgrade guard.

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_0052_*`）中真实验证 0052：

- 升级到 0052 后两张表存在（requests + decisions，含唯一约束）；
- 空表 → downgrade 到 0051 成功且表被移除（无历史数据保留语义，
  closure 表为 P0 新增，降级直接删表）。

全程不触碰主库，finally 恢复 settings 缓存并 DROP 临时库。
"""

import asyncio
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import get_settings
from app.core.runtime import configure_asyncio_runtime
from app.db.session import DatabaseManager

pytestmark = pytest.mark.integration

configure_asyncio_runtime()

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_TABLES = ("backflow_human_review_decisions", "backflow_human_review_requests")

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
            return str((await session.execute(text("SELECT version_num FROM alembic_version"))).scalar_one())
    finally:
        await manager.dispose()

async def _table_exists(temp_url: str, table: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            row = await session.execute(
                text("SELECT 1 FROM information_schema.tables WHERE table_name = :t")
                .bindparams(t=table),
            )
            return row.scalar_one_or_none() is not None
    finally:
        await manager.dispose()

@pytest.mark.asyncio
async def test_0052_upgrade_creates_closure_tables(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0052_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0052")
        assert await _version(temp_url) == "0052"
        for table in _TABLES:
            assert await _table_exists(temp_url, table)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()

@pytest.mark.asyncio
async def test_0052_downgrade_drops_tables_when_empty(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0052_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0052")
        await asyncio.to_thread(command.downgrade, cfg, "0051")
        assert await _version(temp_url) == "0051"
        for table in _TABLES:
            assert not await _table_exists(temp_url, table)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()