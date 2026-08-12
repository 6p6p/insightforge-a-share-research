"""Integration test: migration 0041 research_plans input snapshot downgrade guard.

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0041：

- (B1) **空表通过**：无数据 → `alembic downgrade 0041 -> 0040` 成功、版本回到
  0040、`planner_input_payload` / `planner_input_schema_version` 两列被删；
  随后 `upgrade 0041` 恢复两列与版本（spec W：「完成后再 upgrade 回 0041」）。
- (B2) **new-field-safe 通过**：research_plans 存在纯 v1 行（`plan_schema_version=1`，
  两列全 NULL）→ 两列无数据损失，downgrade 允许删列、行数据保留。
- (A) **v2 snapshot 拒绝**：research_plans 存在 v2 行（`plan_schema_version>=2` +
  `planner_input_payload` 非空）→ `alembic downgrade 0041 -> 0040` 必须拒绝
  （RuntimeError），`alembic_version` 仍为 0041，snapshot 行数据完整保留（immutable
  research planning artifact 不在 downgrade 时静默删除）。

company / research task 复用既有 guard helpers 直接 SQL seed；plan 行直接 SQL 插入
（满足全部 CHECK：fingerprints 用 64 hex、payload 为 JSONB object、
plan_schema_version 与 snapshot 版本一致、名称非空）。

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
_COL_PAYLOAD = "planner_input_payload"
_COL_VERSION = "planner_input_schema_version"


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


async def _column_exists(temp_url: str, column: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='research_plans' "
                        "AND column_name = :col"
                    ).bindparams(col=column)
                )
            ).scalar_one()
            return int(count) == 1
    finally:
        await manager.dispose()


async def _plan_count(temp_url: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int((await session.execute(text(f"SELECT count(*) FROM {_PLANS}"))).scalar_one())
    finally:
        await manager.dispose()


async def _seed_plan(
    temp_url: str,
    *,
    task_id: UUID,
    company_id: UUID,
    plan_schema_version: int = 1,
    planner_input_payload: str | None = None,
) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 research_plans 行。

    v2 行（plan_schema_version=2）必须同时携带 planner_input_payload（JSONB
    object）与 planner_input_schema_version>=1（0041 CHECK）。
    """
    plan_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            if plan_schema_version >= 2:
                assert planner_input_payload is not None
                await session.execute(
                    text(
                        "INSERT INTO research_plans "
                        "(research_plan_id, task_id, company_id, plan_schema_version, "
                        " planner_name, planner_version, model_id, "
                        " planner_input_fingerprint, plan_payload, plan_fingerprint, "
                        " planner_input_payload, planner_input_schema_version) "
                        "VALUES (CAST(:pid AS uuid), CAST(:tid AS uuid), "
                        " CAST(:cid AS uuid), 2, 'research_planner', 1, 'test:fake-model', "
                        " :input_fp, CAST(:payload AS jsonb), :plan_fp, "
                        " CAST(:snap AS jsonb), 1)"
                    ).bindparams(
                        pid=plan_id,
                        tid=task_id,
                        cid=company_id,
                        input_fp=_hex64(),
                        payload='{"research_scope": ["business"]}',
                        plan_fp=_hex64(),
                        snap=planner_input_payload,
                    )
                )
            else:
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


@pytest.mark.asyncio
async def test_migration_0041_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B1) 空表：0041 upgrade 加列；无数据 → downgrade 0041→0040 成功删列；再 upgrade 回 0041。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0041")
        assert await _version(temp_url) == "0041"
        assert await _column_exists(temp_url, _COL_PAYLOAD) is True
        assert await _column_exists(temp_url, _COL_VERSION) is True

        # 空表 → downgrade 0041→0040 成功，两列被删。
        await asyncio.to_thread(command.downgrade, cfg, "0040")
        assert await _version(temp_url) == "0040"
        assert await _column_exists(temp_url, _COL_PAYLOAD) is False
        assert await _column_exists(temp_url, _COL_VERSION) is False

        # 完成后 upgrade 回 0041（spec W）。
        await asyncio.to_thread(command.upgrade, cfg, "0041")
        assert await _version(temp_url) == "0041"
        assert await _column_exists(temp_url, _COL_PAYLOAD) is True
        assert await _column_exists(temp_url, _COL_VERSION) is True
        assert await _plan_count(temp_url) == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0041_downgrade_safe_with_v1_rows(monkeypatch, tmp_path) -> None:
    """(B2) 纯 v1 行（两列全 NULL）→ new-field-safe：允许删列，行数据保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0041")

        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        task_id = await _seed_research_task(temp_url)
        plan_id = await _seed_plan(temp_url, task_id=task_id, company_id=company_id)
        assert await _plan_count(temp_url) == 1

        # v1 行无 snapshot → downgrade 允许（无数据损失）。
        await asyncio.to_thread(command.downgrade, cfg, "0040")
        assert await _version(temp_url) == "0040"
        assert await _column_exists(temp_url, _COL_PAYLOAD) is False
        assert await _plan_count(temp_url) == 1

        # v1 行数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT task_id, company_id, plan_schema_version, "
                            "       planner_name, planner_version, model_id "
                            "FROM research_plans WHERE research_plan_id = :pid"
                        ).bindparams(pid=plan_id)
                    )
                ).one()
                assert row[0] == task_id
                assert row[1] == company_id
                assert row[2] == 1
                assert row[3] == "research_planner"
                assert row[4] == 1
                assert row[5] == "test:fake-model"
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0041_downgrade_blocked_with_v2_snapshot(monkeypatch, tmp_path) -> None:
    """(A) v2 snapshot 行存在 → 拒绝 downgrade；snapshot 数据与版本完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0041")

        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        task_id = await _seed_research_task(temp_url)
        plan_id = await _seed_plan(
            temp_url,
            task_id=task_id,
            company_id=company_id,
            plan_schema_version=2,
            planner_input_payload='{"company_id": "00000000-0000-0000-0000-000000000000"}',
        )
        assert await _plan_count(temp_url) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0041"):
            await asyncio.to_thread(command.downgrade, cfg, "0040")

        # alembic_version 仍 0041，两列保留，snapshot 行数据完整。
        assert await _version(temp_url) == "0041"
        assert await _column_exists(temp_url, _COL_PAYLOAD) is True
        assert await _column_exists(temp_url, _COL_VERSION) is True
        assert await _plan_count(temp_url) == 1

        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT plan_schema_version, planner_input_schema_version "
                            "FROM research_plans WHERE research_plan_id = :pid"
                        ).bindparams(pid=plan_id)
                    )
                ).one()
                assert row[0] == 2
                assert row[1] == 1
                snapshot = (
                    await session.execute(
                        text(
                            "SELECT planner_input_payload->>'company_id' "
                            "FROM research_plans WHERE research_plan_id = :pid"
                        ).bindparams(pid=plan_id)
                    )
                ).scalar_one()
                assert snapshot == "00000000-0000-0000-0000-000000000000"
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
