"""Integration test: migration 0042 research orchestration downgrade guard
(stage 7A.2B.1, spec R).

在**独立临时 PostgreSQL 数据库**中真实验证 0042：

- (A) 0042 upgrade 创建两张表（research_orchestration_runs /
  research_orchestration_child_runs，含全部 CHECK / UNIQUE / INDEX）；无数据 →
  `alembic downgrade 0042 -> 0041` 成功、版本回到 0041、两表被删；
- (B) research_orchestration_runs 存在行 → 拒绝 downgrade（拒绝后 alembic_version
  仍为 0042，orchestration 数据完整保留）；
- (C) research_orchestration_child_runs 存在行 → 拒绝 downgrade（guard 覆盖
  child 表——child 是 persisted ownership linkage，必随 orchestration 存在）。

task / workflow_run 用直接 SQL 插入满足全部 FK；orchestration / child 行用
直接 SQL 插入（input_fingerprint 用 `_hex64` 满足 CHECK 与 UNIQUE）。

测试全程使用 `insightforge_gate_*` 临时库并最终 DROP，不触碰主库
（`insightforge`）。需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB
权限。
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

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_ORCH = "research_orchestration_runs"
_CHILD = "research_orchestration_child_runs"
_TASKS = "research_tasks"
_RUNS = "workflow_runs"

_TABLES = (_ORCH, _CHILD)


def _hex64() -> str:
    """64 位小写 hex（satisfy orchestration input_fingerprint CHECK / UNIQUE）。"""
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


async def _tables_exist(temp_url: str) -> dict[str, bool]:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            rows = (
                (
                    await session.execute(
                        text(
                            "SELECT table_name FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name = ANY(:names)"
                        ).bindparams(names=list(_TABLES))
                    )
                )
                .scalars()
                .all()
            )
            return {table: table in set(rows) for table in _TABLES}
    finally:
        await manager.dispose()


async def _seed_task(temp_url: str) -> UUID:
    """插入一条满足 research_tasks FK 的 task（无 company 依赖）。"""
    task_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_tasks "
                    "(task_id, company_query, research_start_date, research_end_date, "
                    " modules, questions, require_plan_approval) "
                    "VALUES (CAST(:tid AS uuid), '600519', CAST(:start AS date), "
                    " CAST(:end AS date), CAST(:modules AS jsonb), CAST(:questions AS jsonb), "
                    " false)"
                ).bindparams(
                    tid=task_id,
                    start="2023-01-01",
                    end="2026-08-10",
                    modules='["company_profile"]',
                    questions='["分析贵州茅台的经营质量、主要风险和估值水平。"]',
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return task_id


async def _seed_run(temp_url: str, task_id: UUID) -> UUID:
    """插入一条 completed stage4 workflow_runs 行（thread_id 唯一）。"""
    run_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO workflow_runs "
                    "(run_id, task_id, thread_id, graph_name, graph_version, status) "
                    "VALUES (CAST(:rid AS uuid), CAST(:tid AS uuid), :thread, "
                    " 'stage4_analysis', '1', 'completed')"
                ).bindparams(rid=run_id, tid=task_id, thread=str(run_id))
            )
            await session.commit()
    finally:
        await manager.dispose()
    return run_id


async def _seed_orchestration(temp_url: str, task_id: UUID) -> UUID:
    """插入一条 active（running/planning）orchestration 行。"""
    orchestration_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_orchestration_runs "
                    "(orchestration_id, task_id, research_plan_id, "
                    " orchestration_schema_version, orchestrator_name, "
                    " orchestrator_version, status, current_phase, input_fingerprint) "
                    "VALUES (CAST(:oid AS uuid), CAST(:tid AS uuid), NULL, "
                    " 1, 'research_orchestrator', 1, 'running', 'planning', :fp)"
                ).bindparams(oid=orchestration_id, tid=task_id, fp=_hex64())
            )
            await session.commit()
    finally:
        await manager.dispose()
    return orchestration_id


async def _seed_child(temp_url: str, orchestration_id: UUID, run_id: UUID) -> UUID:
    """插入一条 orchestration → stage4 child link。"""
    child_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO research_orchestration_child_runs "
                    "(orchestration_child_id, orchestration_id, workflow_run_id, "
                    " stage, attempt_no, source_research_request_id) "
                    "VALUES (CAST(:cid AS uuid), CAST(:oid AS uuid), CAST(:rid AS uuid), "
                    " 'stage4', 1, NULL)"
                ).bindparams(cid=child_id, oid=orchestration_id, rid=run_id)
            )
            await session.commit()
    finally:
        await manager.dispose()
    return child_id


@pytest.mark.asyncio
async def test_migration_0042_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(A) 0042 upgrade 建两表；无数据 → downgrade 0042→0041 成功，两表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0042")
        assert await _version(temp_url) == "0042"
        exists = await _tables_exist(temp_url)
        assert all(exists[t] for t in _TABLES)
        assert await _table_count(temp_url, _ORCH) == 0
        assert await _table_count(temp_url, _CHILD) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0041")
        assert await _version(temp_url) == "0041"
        exists = await _tables_exist(temp_url)
        assert all(not exists[t] for t in _TABLES)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0042_downgrade_blocked_with_orchestration(monkeypatch) -> None:
    """(B) research_orchestration_runs 存在行 → 拒绝 downgrade，version 保持 0042。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0042")
        task_id = await _seed_task(temp_url)
        orchestration_id = await _seed_orchestration(temp_url, task_id)
        assert await _table_count(temp_url, _ORCH) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0042"):
            await asyncio.to_thread(command.downgrade, cfg, "0041")

        assert await _version(temp_url) == "0042"
        assert await _table_count(temp_url, _ORCH) == 1
        # orchestration 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT status, current_phase FROM research_orchestration_runs "
                            "WHERE orchestration_id = :oid"
                        ).bindparams(oid=orchestration_id)
                    )
                ).one()
                assert row[0] == "running"
                assert row[1] == "planning"
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0042_downgrade_blocked_with_child(monkeypatch) -> None:
    """(C) research_orchestration_child_runs 存在行 → 拒绝 downgrade（guard 覆盖
    child 表）；拒绝后 version 保持 0042、child 数据保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0042")
        task_id = await _seed_task(temp_url)
        orchestration_id = await _seed_orchestration(temp_url, task_id)
        run_id = await _seed_run(temp_url, task_id)
        await _seed_child(temp_url, orchestration_id, run_id)
        assert await _table_count(temp_url, _CHILD) == 1
        assert await _table_count(temp_url, _ORCH) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0042"):
            await asyncio.to_thread(command.downgrade, cfg, "0041")

        assert await _version(temp_url) == "0042"
        assert await _table_count(temp_url, _CHILD) == 1
        assert await _table_count(temp_url, _ORCH) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
