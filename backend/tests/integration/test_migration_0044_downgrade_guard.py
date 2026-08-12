"""Integration test: migration 0044 research backflow supplemental plans downgrade
guard (stage 7A.2B.3).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0044：

- (B) **空表通过**：无数据 → `alembic upgrade head`（=0044）建
  `research_backflow_plans` 表；无数据 → `alembic downgrade 0044 -> 0043` 成功、
  版本回到 0043、`research_backflow_plans` 表被删；
- (A) **非空拒绝**：`research_backflow_plans` 存在行 → `alembic downgrade
  0044 -> 0043` 必须拒绝（RuntimeError），`alembic_version` 仍为 0044；plan 行
  数据完整保留（补充计划是正式 immutable research artifact——记录了 request 对应
  的确定性研究决策 / 派生 query，不在 downgrade 时静默删除历史）。

request 链（company → … → report → audit → workflow run → review action →
backflow request）复用 test_migration_0038 的 seed helpers；plan 行直接 SQL 插入
（满足全部 CHECK：plan_schema_version / strategy_version >= 1、
strategy_name 非空、plan_payload 为 object JSONB、plan_fingerprint 为 64 hex）。

全程不触碰主库 `insightforge`，finally 恢复 settings 缓存并 DROP 临时库。
需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
"""

import asyncio
import json
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0038_downgrade_guard import (
    _seed_full_backflow_chain,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_PLANS = "research_backflow_plans"


def _hex64() -> str:
    """64 位小写 hex（satisfy plan_fingerprint CHECK）。"""
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


async def _table_count(temp_url: str, table: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int((await session.execute(text(f"SELECT count(*) FROM {table}"))).scalar_one())
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


async def _seed_backflow_plan(temp_url: str, request_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_backflow_plans 行。"""
    plan_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_backflow_plans "
                    "(backflow_plan_id, research_backflow_request_id, "
                    " plan_schema_version, strategy_name, strategy_version, "
                    " plan_payload, plan_fingerprint) "
                    "VALUES (CAST(:pid AS uuid), CAST(:req AS uuid), "
                    " 1, 'existing_source_library', 1, "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    pid=plan_id,
                    req=request_id,
                    payload=json.dumps(
                        {
                            "need_specs": [
                                {
                                    "need_code": "insufficient_evidence",
                                    "target_section_ids": ["S2"],
                                    "related_claim_ids": [],
                                    "related_evidence_card_ids": [],
                                    "retrieval_queries": ["贵州茅台 2024 年净利润增长情况？"],
                                    "allowed_source_types": ["annual_report"],
                                }
                            ]
                        },
                        ensure_ascii=False,
                    ),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return plan_id


@pytest.mark.asyncio
async def test_migration_0044_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0044 upgrade 建表；无数据 → downgrade 0044→0043 成功，表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0044")
        assert await _version(temp_url) == "0044"
        assert await _table_exists(temp_url, _PLANS) is True
        assert await _table_count(temp_url, _PLANS) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0043")
        assert await _version(temp_url) == "0043"
        assert await _table_exists(temp_url, _PLANS) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0044_downgrade_blocked_with_plan(monkeypatch, tmp_path) -> None:
    """(A) plan 存在行 → 拒绝 downgrade；数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0044")
        # 先 seed 0038 的完整 backflow 链（company → … → request），满足 FK。
        seeded = await _seed_full_backflow_chain(temp_url, tmp_path)
        plan_id = await _seed_backflow_plan(temp_url, seeded["request_id"])
        assert await _table_count(temp_url, _PLANS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0044"):
            await asyncio.to_thread(command.downgrade, cfg, "0043")

        assert await _version(temp_url) == "0044"
        assert await _table_count(temp_url, _PLANS) == 1
        # plan 行未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT research_backflow_request_id, plan_schema_version, "
                            "strategy_name, strategy_version, plan_fingerprint "
                            "FROM research_backflow_plans WHERE backflow_plan_id = :pid"
                        ).bindparams(pid=plan_id)
                    )
                ).one()
                assert row[0] == seeded["request_id"]
                assert row[1] == 1
                assert row[2] == "existing_source_library"
                assert row[3] == 1
                assert len(row[4]) == 64
                # 上游 backflow request 数据保留。
                requests = int(
                    (
                        await session.execute(
                            text("SELECT count(*) FROM research_backflow_requests")
                        )
                    ).scalar_one()
                )
                assert requests == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
