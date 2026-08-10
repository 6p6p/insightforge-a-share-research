"""Integration test: migration 0031 upgrade/downgrade safety (Stage 4 Final Gate).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0031：

- **upgrade guard**：`workflow_runs` 存在任何 `task_id IS NULL` 的 run →
  `alembic upgrade 0031` 必须拒绝（RuntimeError），`alembic_version` 仍为
  0030，数据不丢；清理 NULL 行后再 upgrade → 成功到 0031；
- **downgrade**：0031 → 0030 恢复 `task_id` nullable，不删除任何数据。

全程不触碰主库 `insightforge`，finally 恢复 settings 缓存并 DROP 临时库。
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

_GUARD_REASON = "refusing to restore NOT NULL silently"


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


async def _run_sql(temp_url: str, sql: str, **params) -> None:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(text(sql).bindparams(**params))
            await session.commit()
    finally:
        await manager.dispose()


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


async def _null_task_run_count(temp_url: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int(
                (
                    await session.execute(
                        text("SELECT count(*) FROM workflow_runs WHERE task_id IS NULL")
                    )
                ).scalar_one()
            )
    finally:
        await manager.dispose()


async def _task_id_nullable(temp_url: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            value = (
                await session.execute(
                    text(
                        "SELECT is_nullable FROM information_schema.columns "
                        "WHERE table_name = 'workflow_runs' AND column_name = 'task_id'"
                    )
                )
            ).scalar_one()
            return str(value) == "YES"
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_0031_upgrade_guard_blocks_null_task_id_runs(monkeypatch) -> None:
    """存在 task_id IS NULL 的 run → 拒绝 upgrade；清理后 → 正常 upgrade。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0030")

        # seed 一个 task_id IS NULL 的 run（0030 语义：nullable）。
        await _run_sql(
            temp_url,
            "INSERT INTO workflow_runs (run_id, task_id, thread_id, graph_name, graph_version) "
            "VALUES (:rid, NULL, :tid, 'stage4_analysis', '1')",
            rid=uuid4(),
            tid=str(uuid4()),
        )
        assert await _null_task_run_count(temp_url) == 1

        # 有 NULL 行 → upgrade 0031 必须拒绝，且不猜任务 / 不自动绑定。
        with pytest.raises(RuntimeError, match=_GUARD_REASON):
            await asyncio.to_thread(command.upgrade, cfg, "0031")
        assert await _version(temp_url) == "0030"
        assert await _null_task_run_count(temp_url) == 1

        # 清理 NULL 行后 → upgrade 0031 成功（NOT NULL 恢复）。
        await _run_sql(temp_url, "DELETE FROM workflow_runs WHERE task_id IS NULL")
        await asyncio.to_thread(command.upgrade, cfg, "0031")
        assert await _version(temp_url) == "0031"
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_0031_downgrade_restores_nullable(monkeypatch) -> None:
    """0031 → 0030：task_id 回到 nullable；再 upgrade 回 0031 成功。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0031")
        assert await _version(temp_url) == "0031"
        # 0031 下 task_id NOT NULL。
        assert await _task_id_nullable(temp_url) is False

        await asyncio.to_thread(command.downgrade, cfg, "0030")
        assert await _version(temp_url) == "0030"
        # 0030 语义恢复：task_id 回到 nullable。
        assert await _task_id_nullable(temp_url) is True
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
