"""Integration test: migration 0045 evaluation execution persistence downgrade
guard (stage 7B.1.3A, spec S).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0045：

- (B) **空表通过**：无数据 → `alembic upgrade head`（=0045）建四张表
  （`eval_execution_specs` / `eval_trials` / `eval_execution_attempts` /
  `eval_llm_call_usages`）；无数据 → `alembic downgrade 0044` 成功、版本回到
  0044、四张表被删；
- (A) **非空拒绝**：`eval_execution_specs` 存在行 → `alembic downgrade 0044`
  必须拒绝（RuntimeError），`alembic_version` 仍为 0045；spec 行数据完整保留
  （执行历史是正式 immutable eval artifact，不在 downgrade 时静默删除）。

`eval_execution_specs` 是四层的根表（无上游 FK），直接 SQL 插入一条满足全部
CHECK 的 spec 行即可触发 guard，无需构造真实 case/snapshot。

全程不触碰主库 `insightforge`，finally 恢复 settings 缓存并 DROP 临时库。
需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
"""

import asyncio
import json
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

_EVAL_TABLES = (
    "eval_execution_specs",
    "eval_trials",
    "eval_execution_attempts",
    "eval_llm_call_usages",
)


def _hex64() -> str:
    return uuid4().hex + uuid4().hex


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


async def _table_exists(temp_url: str, table: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            exists = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_schema='public' AND table_name = :name"
                    ).bindparams(name=table)
                )
            ).scalar_one()
            return int(exists) == 1
    finally:
        await manager.dispose()


async def _table_count(temp_url: str, table: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
    finally:
        await manager.dispose()


async def _seed_execution_spec(temp_url: str) -> str:
    """直接 SQL 插入一条满足全部 CHECK 的 eval_execution_specs 行。"""
    spec_id = uuid4()
    fingerprint = _hex64()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO eval_execution_specs "
                    "(execution_spec_id, schema_version, execution_spec_fingerprint, variant_id, "
                    " case_fingerprint, source_snapshot_fingerprint, execution_config_fingerprint, "
                    " execution_spec_payload, execution_config_payload) "
                    "VALUES (CAST(:sid AS uuid), 1, :fp, 'insightforge_full', "
                    " :fp, :fp, :fp, CAST(:spayload AS jsonb), CAST(:cpayload AS jsonb))"
                ).bindparams(
                    sid=spec_id,
                    fp=fingerprint,
                    spayload=json.dumps({"schema_version": 1}, ensure_ascii=False),
                    cpayload=json.dumps({"config_schema_version": 1}, ensure_ascii=False),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return fingerprint


@pytest.mark.asyncio
async def test_migration_0045_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0045 upgrade 建四张表；无数据 → downgrade 0044 成功，四张表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0045")
        assert await _version(temp_url) == "0045"
        for table in _EVAL_TABLES:
            assert await _table_exists(temp_url, table) is True
            assert await _table_count(temp_url, table) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0044")
        assert await _version(temp_url) == "0044"
        for table in _EVAL_TABLES:
            assert await _table_exists(temp_url, table) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0045_downgrade_blocked_with_spec(monkeypatch) -> None:
    """(A) spec 存在行 → 拒绝 downgrade；数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0045")
        fingerprint = await _seed_execution_spec(temp_url)
        assert await _table_count(temp_url, "eval_execution_specs") == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0045"):
            await asyncio.to_thread(command.downgrade, cfg, "0044")

        assert await _version(temp_url) == "0045"
        assert await _table_count(temp_url, "eval_execution_specs") == 1
        # spec 行未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT execution_spec_fingerprint, variant_id "
                            "FROM eval_execution_specs LIMIT 1"
                        )
                    )
                ).one()
                assert row[0] == fingerprint
                assert row[1] == "insightforge_full"
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
