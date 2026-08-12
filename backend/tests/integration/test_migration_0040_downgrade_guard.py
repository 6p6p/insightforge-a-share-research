"""Integration test: migration 0040 research_plans downgrade guard (stage 7A.1 W).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0040：

- (B) **空表通过**：无数据 → `alembic downgrade 0040 -> 0039` 成功、版本回到
  0039、`research_plans` / `research_plan_routes` 表被删；随后 `upgrade 0040`
  恢复表与版本（spec W：「完成后再 upgrade 回 0040」）。
- (A) **非空拒绝**：`research_plans` 存在行（含其 route 行）→ `alembic
  downgrade 0040 -> 0039` 必须拒绝（RuntimeError），`alembic_version` 仍为
  0040，plan / route 行数据完整保留（Plan 与 Route 是正式 immutable research
  planning artifact——即使可确定性重放，也不在 downgrade 时静默删除历史）。

company / research task 复用既有 guard helpers 直接 SQL seed；plan / route 行
直接 SQL 插入（满足全部 CHECK：fingerprints 用 64 hex、payload 为 JSONB
object、schema/version >= 1、名称非空）。

全程不触碰主库 `insightforge`，finally 恢复 settings 缓存并 DROP 临时库。
需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
"""

import asyncio
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
from tests.integration.test_migration_0026_downgrade_guard import _seed_chain
from tests.integration.test_migration_0028_downgrade_guard import _hex64
from tests.integration.test_migration_0038_downgrade_guard import _seed_research_task

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_PLANS = "research_plans"
_ROUTES = "research_plan_routes"


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


async def _seed_plan(temp_url: str, *, task_id: UUID, company_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_plans 行。"""
    plan_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_plans "
                    "(research_plan_id, task_id, company_id, plan_schema_version, "
                    " planner_name, planner_version, model_id, "
                    " planner_input_fingerprint, plan_payload, plan_fingerprint) "
                    "VALUES (CAST(:pid AS uuid), CAST(:tid AS uuid), "
                    " CAST(:cid AS uuid), 1, 'research_planner', 1, 'test:fake-model', "
                    " :input_fp, CAST(:payload AS jsonb), :plan_fp)"
                ).bindparams(
                    pid=plan_id,
                    tid=task_id,
                    cid=company_id,
                    input_fp=_hex64(),
                    payload='{"research_scope": ["business"]}',
                    plan_fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return plan_id


async def _seed_route(temp_url: str, *, research_plan_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_plan_routes 行。"""
    route_plan_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_plan_routes "
                    "(route_plan_id, research_plan_id, route_schema_version, "
                    " router_name, router_version, route_payload, route_fingerprint) "
                    "VALUES (CAST(:rid AS uuid), CAST(:pid AS uuid), 1, "
                    " 'source_router', 1, CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    rid=route_plan_id,
                    pid=research_plan_id,
                    payload='{"entries": []}',
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return route_plan_id


@pytest.mark.asyncio
async def test_migration_0040_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0040 upgrade 建表；无数据 → downgrade 0040→0039 成功，表被删；再 upgrade 回 0040。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0040")
        assert await _version(temp_url) == "0040"
        assert await _table_exists(temp_url, _PLANS) is True
        assert await _table_exists(temp_url, _ROUTES) is True
        assert await _table_count(temp_url, _PLANS) == 0
        assert await _table_count(temp_url, _ROUTES) == 0

        # 空表 → downgrade 0040→0039 成功。
        await asyncio.to_thread(command.downgrade, cfg, "0039")
        assert await _version(temp_url) == "0039"
        assert await _table_exists(temp_url, _PLANS) is False
        assert await _table_exists(temp_url, _ROUTES) is False

        # 完成后 upgrade 回 0040（spec W）。
        await asyncio.to_thread(command.upgrade, cfg, "0040")
        assert await _version(temp_url) == "0040"
        assert await _table_exists(temp_url, _PLANS) is True
        assert await _table_exists(temp_url, _ROUTES) is True
        assert await _table_count(temp_url, _PLANS) == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0040_downgrade_blocked_with_plan(monkeypatch, tmp_path) -> None:
    """(A) research_plans 存在行（含 route 行）→ 拒绝 downgrade；行数据与版本完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0040")

        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        task_id = await _seed_research_task(temp_url)
        plan_id = await _seed_plan(temp_url, task_id=task_id, company_id=company_id)
        route_id = await _seed_route(temp_url, research_plan_id=plan_id)
        assert await _table_count(temp_url, _PLANS) == 1
        assert await _table_count(temp_url, _ROUTES) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0040"):
            await asyncio.to_thread(command.downgrade, cfg, "0039")

        # alembic_version 仍 0040，表与行保留。
        assert await _version(temp_url) == "0040"
        assert await _table_exists(temp_url, _PLANS) is True
        assert await _table_exists(temp_url, _ROUTES) is True
        assert await _table_count(temp_url, _PLANS) == 1
        assert await _table_count(temp_url, _ROUTES) == 1

        # plan / route 行数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                plan = (
                    await session.execute(
                        text(
                            "SELECT task_id, company_id, plan_schema_version, "
                            "       planner_name, planner_version, model_id "
                            "FROM research_plans WHERE research_plan_id = :pid"
                        ).bindparams(pid=plan_id)
                    )
                ).one()
                assert plan[0] == task_id
                assert plan[1] == company_id
                assert plan[2] == 1
                assert plan[3] == "research_planner"
                assert plan[4] == 1
                assert plan[5] == "test:fake-model"

                route = (
                    await session.execute(
                        text(
                            "SELECT research_plan_id, route_schema_version, "
                            "       router_name, router_version "
                            "FROM research_plan_routes WHERE route_plan_id = :rid"
                        ).bindparams(rid=route_id)
                    )
                ).one()
                assert route[0] == plan_id
                assert route[1] == 1
                assert route[2] == "source_router"
                assert route[3] == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
