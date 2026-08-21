"""Integration test: migration 0055 orchestration completed_with_warnings status.

在独立临时 PostgreSQL 数据库（`insightforge_gate_0055_*`）中真实验证 0055：
- 升级到 0055 后 `ck_ro_status` 约束允许 `completed_with_warnings`；
- downgrade 到 0054 后约束收紧、不再包含 `completed_with_warnings`。

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
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=15)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            stmt = text("SELECT version_num FROM alembic_version")
            row = (await session.execute(stmt)).scalar_one()
            return str(row)
    finally:
        await manager.dispose()


async def _status_constraint_sql(temp_url: str) -> str:
    """读取 ck_ro_status CheckConstraint 的表达式文本（pg_constraint）。"""
    manager = DatabaseManager(
        database_url=temp_url,
        echo=False,
        connect_timeout_seconds=15,
    )
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            result = await session.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'ck_ro_status' AND conrelid = "
                    "'research_orchestration_runs'::regclass"
                )
            )
            row = result.first()
            if row is None:
                return ""
            return str(row[0])
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_0055_upgrade_allows_completed_with_warnings(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0055_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0055")
        assert await _version(temp_url) == "0055"
        constraint = await _status_constraint_sql(temp_url)
        assert "completed_with_warnings" in constraint
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0055_downgrade_rejects_completed_with_warnings(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_0055_{uuid4().hex[:10]}"
    temp_url = _temp_url(base_url, temp_db)
    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0055")
        await asyncio.to_thread(command.downgrade, cfg, "0054")
        assert await _version(temp_url) == "0054"
        # CHECK 收紧：completed_with_warnings 不在约束内。
        constraint = await _status_constraint_sql(temp_url)
        assert "completed_with_warnings" not in constraint
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
