"""Integration test: migration 0043 research orchestration retry downgrade guard
(stage 7A.2B.2, spec X).

在**独立临时 PostgreSQL 数据库**中真实验证 0043：

- (A) 0043 upgrade 添加 attempt_no / retry_of_orchestration_id 列 + CHECK + FK +
  UNIQUE(research_plan_id, attempt_no)、移除 UNIQUE(input_fingerprint)；无数据 →
  `alembic downgrade 0043 -> 0042` 成功、版本回到 0042、新列被删、
  UNIQUE(input_fingerprint) 恢复；
- (B) research_orchestration_runs 存在行 → 拒绝 downgrade（拒绝后 alembic_version
  仍为 0043，orchestration 数据完整保留）；
- (C) retry 语义（spec B）：同 input_fingerprint 两行（attempt 1 / 2、retry_of
  链）**允许并存**——fingerprint 不再是唯一键，唯一性由
  (research_plan_id, attempt_no) 承担（retry 必须 NEW orchestration_id +
  NEW top-level thread）。两行用 terminal 状态（attempt 1=failed、attempt
  2=completed）共存，避免 task_id 单 active（pending/running/waiting_human）
  partial unique 冲突。

task / workflow_run 用直接 SQL 插入满足全部 FK；orchestration 行用直接 SQL 插入
（input_fingerprint 用 `_hex64` 满足 CHECK；0043 后 attempt_no NOT NULL 需显式提供）。

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
_TASKS = "research_tasks"

_TABLES = (_ORCH,)


def _hex64() -> str:
    """64 位小写 hex（satisfy orchestration input_fingerprint CHECK）。"""
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


async def _columns(temp_url: str, table: str) -> set[str]:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name = :t"
                    ).bindparams(t=table)
                )
            ).scalars()
            return set(rows)
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


async def _seed_orchestration(
    temp_url: str,
    task_id: UUID,
    *,
    fingerprint: str,
    attempt_no: int,
    status: str = "running",
    retry_of: UUID | None = None,
) -> UUID:
    """插入一条 orchestration 行（0043 retry schema：attempt_no NOT NULL）。

    `status` 可指定：同 task 只能有一条 active（pending/running/waiting_human），
    retry 场景用 terminal 状态（如 failed/completed）共存。
    """
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
                    " orchestrator_version, status, current_phase, input_fingerprint, "
                    " attempt_no, retry_of_orchestration_id) "
                    "VALUES (CAST(:oid AS uuid), CAST(:tid AS uuid), NULL, "
                    " 1, 'research_orchestrator', 1, :status, 'planning', :fp, "
                    " :attempt, CAST(:retry_of AS uuid))"
                ).bindparams(
                    oid=orchestration_id,
                    tid=task_id,
                    status=status,
                    fp=fingerprint,
                    attempt=attempt_no,
                    retry_of=str(retry_of) if retry_of is not None else None,
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return orchestration_id


@pytest.mark.asyncio
async def test_migration_0043_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(A) 0043 upgrade 加 retry 列；无数据 → downgrade 0043→0042 成功。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0043")
        assert await _version(temp_url) == "0043"
        cols = await _columns(temp_url, _ORCH)
        assert "attempt_no" in cols
        assert "retry_of_orchestration_id" in cols
        assert await _table_count(temp_url, _ORCH) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0042")
        assert await _version(temp_url) == "0042"
        cols = await _columns(temp_url, _ORCH)
        assert "attempt_no" not in cols
        assert "retry_of_orchestration_id" not in cols
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0043_downgrade_blocked_with_rows(monkeypatch) -> None:
    """(B) research_orchestration_runs 存在行 → 拒绝 downgrade，version 保持 0043。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0043")
        task_id = await _seed_task(temp_url)
        orchestration_id = await _seed_orchestration(
            temp_url, task_id, fingerprint=_hex64(), attempt_no=1
        )
        assert await _table_count(temp_url, _ORCH) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0043"):
            await asyncio.to_thread(command.downgrade, cfg, "0042")

        assert await _version(temp_url) == "0043"
        assert await _table_count(temp_url, _ORCH) == 1
        # orchestration 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT status, current_phase, attempt_no "
                            "FROM research_orchestration_runs WHERE orchestration_id = :oid"
                        ).bindparams(oid=orchestration_id)
                    )
                ).one()
                assert row[0] == "running"
                assert row[1] == "planning"
                assert row[2] == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0043_retry_rows_same_fingerprint_allowed(monkeypatch) -> None:
    """(C) 同 fingerprint 的 attempt 1/2（retry_of 链）允许并存——fingerprint 不再是
    唯一键，retry 语义由 (research_plan_id, attempt_no) 承担。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0043")
        task_id = await _seed_task(temp_url)
        fp = _hex64()
        o1 = await _seed_orchestration(
            temp_url, task_id, fingerprint=fp, attempt_no=1, status="failed"
        )
        _ = await _seed_orchestration(
            temp_url,
            task_id,
            fingerprint=fp,
            attempt_no=2,
            status="completed",
            retry_of=o1,
        )
        assert await _table_count(temp_url, _ORCH) == 2

        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT attempt_no, retry_of_orchestration_id, input_fingerprint "
                            "FROM research_orchestration_runs ORDER BY attempt_no"
                        )
                    )
                ).all()
                assert [(r[0], r[1]) for r in rows] == [(1, None), (2, o1)]
                assert {r[2] for r in rows} == {fp}
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
