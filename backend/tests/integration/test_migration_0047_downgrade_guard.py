"""Integration test: migration 0047 eval scoring persistence downgrade guard.

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0047：

- 升级到 0047 后六张 scoring 表存在；
- 六张表任一存在行 → `alembic downgrade 0046` 拒绝（RuntimeError），
  `alembic_version` 仍为 0047，行数据完整保留；
- 全部为空 → downgrade 成功。

全程不触碰主库，finally 恢复 settings 缓存并 DROP 临时库。
需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
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
from app.db.session import DatabaseManager

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_TABLES = (
    "eval_scoring_specs",
    "eval_score_runs",
    "eval_metric_values",
    "eval_human_label_bindings",
    "eval_judge_runs",
    "eval_judge_metric_results",
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


async def _insert_scoring_spec_row(temp_url: str) -> str:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            spec_id = str(uuid4())
            await session.execute(
                text(
                    "INSERT INTO eval_scoring_specs "
                    "(scoring_spec_id, schema_version, scoring_spec_fingerprint, "
                    " variant_output_fingerprint, metric_registry_version, payload) "
                    "VALUES (CAST(:sid AS uuid), 1, :fp, :vofp, 1, '{}'::jsonb)"
                ).bindparams(sid=spec_id, fp="a" * 64, vofp="b" * 64)
            )
            await session.commit()
            return spec_id
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_0047_upgrade_creates_scoring_tables(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0047")
        assert await _version(temp_url) == "0047"
        for table in _TABLES:
            assert await _table_has_row(temp_url, table) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0047_downgrade_blocked_with_rows(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0047")
        await _insert_scoring_spec_row(temp_url)
        assert await _table_has_row(temp_url, "eval_scoring_specs") is True

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0047"):
            await asyncio.to_thread(command.downgrade, cfg, "0046")

        assert await _version(temp_url) == "0047"
        assert await _table_has_row(temp_url, "eval_scoring_specs") is True
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0047_downgrade_succeeds_when_empty(monkeypatch) -> None:
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0047")
        await asyncio.to_thread(command.downgrade, cfg, "0046")
        assert await _version(temp_url) == "0046"
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
