"""Integration test: migration 0027 relative valuation claim provenance downgrade guard
(stage 4C.2B.1, spec V).

在**独立临时 PostgreSQL 数据库**中真实验证 0027：

- (A) 0027 upgrade 创建两张表（claim_relative_valuation_comparison_links /
  relative_valuation_claim_profiles，含全部 CHECK / UNIQUE / INDEX）；无数据 →
  `alembic downgrade 0027 -> 0026` 成功、版本回到 0026、两表被删；
- (B) relative_valuation_claim_profiles 存在行 → 拒绝 downgrade（拒绝后
  alembic_version 仍为 0027，数据完整保留，不删除 / 不改写 / 不丢弃 assessment）；
- (C) claim_relative_valuation_comparison_links 存在行 → 拒绝 downgrade（guard
  覆盖 link 表）。

company / Evidence / valuation observation / comparison 用真实服务链 seed
（复用 0026 guard 测试的 helpers）；claim 行直接 SQL 插入（fingerprint /
research_question_sha256 用生产函数计算，满足全部 CHECK）。profile / link 行
直接 SQL 插入（满足 CHECK）。

测试全程使用 `insightforge_gate_*` 临时库并最终 DROP，不触碰主库
（`insightforge`）。需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
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
from app.claims.contracts import compute_claim_fingerprint, compute_research_question_sha256
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0026_downgrade_guard import (
    _ANALYSIS_AS_OF,
    _seed_chain,
    _seed_comparison,
    _seed_observation,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_LINKS = "claim_relative_valuation_comparison_links"
_PROFILES = "relative_valuation_claim_profiles"

_TABLES = (_LINKS, _PROFILES)

_QUESTION = "2026年贵州茅台估值水平？"
_STATEMENT = "贵州茅台当前估值水平与可比公司基本一致。"


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


def _claim_fingerprint(company_id: UUID) -> str:
    return compute_claim_fingerprint(
        claim_schema_version=1,
        company_id=company_id,
        research_question=_QUESTION,
        statement=_STATEMENT,
        analysis_domain="valuation",
        claim_kind="relative_valuation",
        confidence="medium",
        importance="normal",
        analyst_name="test-analyst",
        analyst_version=1,
        analyst_model_id=None,
        supports=[],
        contradicts=[],
        context=[],
    )


async def _seed_claim(temp_url: str, chain: dict) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 claims 行（v1 通用 fingerprint）。"""
    claim_id = uuid4()
    company_id = UUID(chain["company_id"])
    rq_sha = compute_research_question_sha256(_QUESTION)
    fp = _claim_fingerprint(company_id)
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO claims "
                    "(claim_id, company_id, research_question, research_question_sha256, "
                    " statement, analysis_domain, claim_kind, confidence, importance, "
                    " analyst_name, analyst_version, claim_schema_version, claim_fingerprint) "
                    "VALUES (CAST(:cid AS uuid), CAST(:company_id AS uuid), :question, :rq_sha, "
                    " :statement, 'valuation', 'relative_valuation', 'medium', 'normal', "
                    " 'test-analyst', 1, 7, :fp)"
                ).bindparams(
                    cid=claim_id,
                    company_id=company_id,
                    question=_QUESTION,
                    rq_sha=rq_sha,
                    statement=_STATEMENT,
                    fp=fp,
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return claim_id


async def _seed_profile(temp_url: str, claim_id: UUID) -> None:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO relative_valuation_claim_profiles "
                    "(claim_id, assessment, analysis_as_of, profile_schema_version) "
                    "VALUES (CAST(:cid AS uuid), 'mixed', CAST(:asof AS date), 1)"
                ).bindparams(cid=claim_id, asof=_ANALYSIS_AS_OF)
            )
            await session.commit()
    finally:
        await manager.dispose()


async def _seed_link(temp_url: str, claim_id: UUID, comparison_id: str) -> None:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO claim_relative_valuation_comparison_links "
                    "(claim_id, comparison_id, relation) "
                    "VALUES (CAST(:cid AS uuid), CAST(:cmp AS uuid), 'supports')"
                ).bindparams(cid=claim_id, cmp=UUID(comparison_id))
            )
            await session.commit()
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0027_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(A) 0027 upgrade 建两表；无数据 → downgrade 0027→0026 成功，两表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0027")
        assert await _version(temp_url) == "0027"
        exists = await _tables_exist(temp_url)
        assert all(exists[t] for t in _TABLES)
        assert await _table_count(temp_url, _LINKS) == 0
        assert await _table_count(temp_url, _PROFILES) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0026")
        assert await _version(temp_url) == "0026"
        exists = await _tables_exist(temp_url)
        assert all(not exists[t] for t in _TABLES)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0027_downgrade_blocked_with_profile(monkeypatch, tmp_path) -> None:
    """(B) relative_valuation_claim_profiles 存在行 → 拒绝 downgrade，
    alembic_version 保持 0027，数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0027")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        claim_id = await _seed_claim(temp_url, chain)
        await _seed_profile(temp_url, claim_id)
        assert await _table_count(temp_url, _PROFILES) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0027"):
            await asyncio.to_thread(command.downgrade, cfg, "0026")

        assert await _version(temp_url) == "0027"
        assert await _table_count(temp_url, _PROFILES) == 1
        assert await _table_count(temp_url, _LINKS) == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0027_downgrade_blocked_with_link(monkeypatch, tmp_path) -> None:
    """(C) claim_relative_valuation_comparison_links 存在行 → 拒绝 downgrade。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0027")
        # comparison 需要 4 个 observation（target + 3 peers）链。
        target_chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        target = await _seed_observation(temp_url, target_chain, value="15.3")
        peers = []
        for i, value in enumerate(["14.2", "15.0", "16.0"]):
            chain = await _seed_chain(temp_url, tmp_path / "raw", f"6005{2 + i:02d}")
            obs = await _seed_observation(temp_url, chain, value=value)
            obs["company_id"] = chain["company_id"]
            obs["value"] = value
            peers.append(obs)
        comparison_id = await _seed_comparison(temp_url, target, peers)
        claim_id = await _seed_claim(temp_url, target_chain)
        await _seed_link(temp_url, claim_id, comparison_id)
        assert await _table_count(temp_url, _LINKS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0027"):
            await asyncio.to_thread(command.downgrade, cfg, "0026")

        assert await _version(temp_url) == "0027"
        assert await _table_count(temp_url, _LINKS) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
