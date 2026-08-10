"""Integration test: migration 0028 claim synthesis input foundation downgrade
guard (stage 4D.1A, spec U).

在**独立临时 PostgreSQL 数据库**中真实验证 0028：

- (A) 0028 upgrade 创建两张表（claim_synthesis_runs /
  claim_synthesis_input_links，含全部 CHECK / UNIQUE / INDEX）；无数据 →
  `alembic downgrade 0028 -> 0027` 成功、版本回到 0027、两表被删；
- (B) claim_synthesis_runs 存在行 → 拒绝 downgrade（拒绝后 alembic_version
  仍为 0028，run 数据完整保留）；
- (C) claim_synthesis_input_links 存在行 → 拒绝 downgrade（guard 覆盖 link
  表——link 必随 run 存在，两表都被 guard 覆盖）。

company / evidence 用真实服务链 seed（复用 0026 guard 测试的 `_seed_chain`）；
claim / run / link 行直接 SQL 插入（fingerprint / research_question_sha256 用
生产函数计算，满足全部 CHECK）。

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
from app.claims.contracts import compute_research_question_sha256
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0026_downgrade_guard import (
    _ANALYSIS_AS_OF,
    _seed_chain,
)
from tests.integration.test_migration_0027_downgrade_guard import _seed_claim

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_RUNS = "claim_synthesis_runs"
_LINKS = "claim_synthesis_input_links"

_TABLES = (_RUNS, _LINKS)

_QUESTION = "贵州茅台2026年营收与估值是否合理？"


def _hex64() -> str:
    """64 位小写 hex（satisfy claims / runs 表 CHECK 与 UNIQUE）。"""
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


async def _seed_run(temp_url: str, company_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 claim_synthesis_runs 行。"""
    synthesis_id = uuid4()
    rq_sha = compute_research_question_sha256(_QUESTION)
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO claim_synthesis_runs "
                    "(synthesis_id, company_id, research_question, "
                    " research_question_sha256, analysis_as_of, "
                    " synthesis_schema_version, synthesis_fingerprint) "
                    "VALUES (CAST(:sid AS uuid), CAST(:company_id AS uuid), :question, "
                    " :rq_sha, CAST(:asof AS date), 1, :fp)"
                ).bindparams(
                    sid=synthesis_id,
                    company_id=company_id,
                    question=_QUESTION,
                    rq_sha=rq_sha,
                    asof=_ANALYSIS_AS_OF,
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return synthesis_id


async def _seed_link(temp_url: str, synthesis_id: UUID, claim_id: UUID) -> None:
    """直接 SQL 插入一条 claim_synthesis_input_links 行。"""
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO claim_synthesis_input_links (synthesis_id, claim_id) "
                    "VALUES (CAST(:sid AS uuid), CAST(:cid AS uuid))"
                ).bindparams(sid=synthesis_id, cid=claim_id)
            )
            await session.commit()
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0028_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(A) 0028 upgrade 建两表；无数据 → downgrade 0028→0027 成功，两表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0028")
        assert await _version(temp_url) == "0028"
        exists = await _tables_exist(temp_url)
        assert all(exists[t] for t in _TABLES)
        assert await _table_count(temp_url, _RUNS) == 0
        assert await _table_count(temp_url, _LINKS) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0027")
        assert await _version(temp_url) == "0027"
        exists = await _tables_exist(temp_url)
        assert all(not exists[t] for t in _TABLES)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0028_downgrade_blocked_with_run(monkeypatch, tmp_path) -> None:
    """(B) claim_synthesis_runs 存在行 → 拒绝 downgrade，alembic_version 保持
    0028，run 数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0028")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        synthesis_id = await _seed_run(temp_url, UUID(chain["company_id"]))
        assert await _table_count(temp_url, _RUNS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0028"):
            await asyncio.to_thread(command.downgrade, cfg, "0027")

        assert await _version(temp_url) == "0028"
        assert await _table_count(temp_url, _RUNS) == 1
        assert await _table_count(temp_url, _LINKS) == 0
        # run 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT synthesis_schema_version FROM claim_synthesis_runs "
                            "WHERE synthesis_id = :sid"
                        ).bindparams(sid=synthesis_id)
                    )
                ).scalar_one()
                assert int(row) == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0028_downgrade_blocked_with_link(monkeypatch, tmp_path) -> None:
    """(C) claim_synthesis_input_links 存在行 → 拒绝 downgrade（guard 覆盖 link
    表）；拒绝后 version 保持 0028、link 数据保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0028")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, UUID(chain["company_id"]))
        await _seed_link(temp_url, synthesis_id, claim_id)
        assert await _table_count(temp_url, _LINKS) == 1
        assert await _table_count(temp_url, _RUNS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0028"):
            await asyncio.to_thread(command.downgrade, cfg, "0027")

        assert await _version(temp_url) == "0028"
        assert await _table_count(temp_url, _LINKS) == 1
        assert await _table_count(temp_url, _RUNS) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
