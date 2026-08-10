"""Integration test: migration 0032 report outline downgrade guard (stage 5A gate).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0032：

- (A) **非空拒绝**：`report_outlines` 存在行 → `alembic downgrade 0032 -> 0031`
  必须拒绝（RuntimeError），`alembic_version` 仍为 0032，行数据完整保留；
- (B) **空表通过**：无数据 → `alembic downgrade 0032 -> 0031` 成功、版本回到
  0031、表被删。

ReportOutline 是正式 immutable research artifact，即使可确定性重放，也不在
downgrade 时静默删除历史（spec A：downgrade 语义不接受 simple drop）。

company / evidence / claim / synthesis run / input link / result 用真实服务链
seed（复用既有 guard 测试 helpers）；result / outline 行直接 SQL 插入（满足
全部 CHECK：fingerprint 用生产函数计算，payload 为合法 JSONB）。

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
from app.claims.contracts import compute_research_question_sha256
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0026_downgrade_guard import (
    _ANALYSIS_AS_OF,
    _seed_chain,
)
from tests.integration.test_migration_0027_downgrade_guard import _seed_claim
from tests.integration.test_migration_0028_downgrade_guard import (
    _QUESTION,
    _hex64,
    _seed_link,
    _seed_run,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_OUTLINES = "report_outlines"
_RESULTS = "claim_synthesis_results"


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


async def _seed_result(temp_url: str, synthesis_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 claim_synthesis_results 行。"""
    result_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO claim_synthesis_results "
                    "(synthesis_result_id, synthesis_id, result_schema_version, "
                    " result_fingerprint, themes, claim_roles, duplicates, conflicts, "
                    " evidence_gaps, summary, analyst_name, analyst_version, analyst_model_id) "
                    "VALUES (CAST(:rid AS uuid), CAST(:sid AS uuid), 1, :fp, "
                    " CAST(:themes AS jsonb), CAST(:claim_roles AS jsonb), "
                    " CAST(:duplicates AS jsonb), CAST(:conflicts AS jsonb), "
                    " CAST(:gaps AS jsonb), :summary, "
                    " 'structured_claim_synthesis_analyst', 1, 'deepseek:deepseek-v4-flash')"
                ).bindparams(
                    rid=result_id,
                    sid=synthesis_id,
                    fp=_hex64(),
                    themes='[{"title":"主题","summary":"摘要","claim_refs":["C1"]}]',
                    claim_roles='[{"claim_ref":"C1","role":"support","rationale":"支持"}]',
                    duplicates="[]",
                    conflicts="[]",
                    gaps="[]",
                    summary="综合总结。",
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return result_id


async def _seed_outline(temp_url: str, result_id: UUID, company_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 report_outlines 行。"""
    outline_id = uuid4()
    rq_sha = compute_research_question_sha256(_QUESTION)
    payload = {
        "sections": [
            {
                "section_id": "S1",
                "section_type": "theme",
                "title": "主题",
                "claim_ids": [],
                "conflict_indexes": [],
                "evidence_gap_indexes": [],
                "section_order": 1,
            }
        ]
    }
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO report_outlines "
                    "(outline_id, synthesis_result_id, company_id, research_question_sha256, "
                    " analysis_as_of, outline_schema_version, outline_payload, "
                    " outline_fingerprint) "
                    "VALUES (CAST(:oid AS uuid), CAST(:rid AS uuid), "
                    " CAST(:company_id AS uuid), :rq_sha, CAST(:asof AS date), 1, "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    oid=outline_id,
                    rid=result_id,
                    company_id=company_id,
                    rq_sha=rq_sha,
                    asof=_ANALYSIS_AS_OF,
                    payload=json.dumps(payload, ensure_ascii=False),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return outline_id


@pytest.mark.asyncio
async def test_migration_0032_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0032 upgrade 建表；无数据 → downgrade 0032→0031 成功，表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0032")
        assert await _version(temp_url) == "0032"
        assert await _table_count(temp_url, _OUTLINES) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0031")
        assert await _version(temp_url) == "0031"
        # 0031 之下 report_outlines 表已删除。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                still = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name = :name"
                        ).bindparams(name=_OUTLINES)
                    )
                ).scalar_one()
                assert int(still) == 0
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0032_downgrade_blocked_with_outline(monkeypatch, tmp_path) -> None:
    """(A) report_outlines 存在行 → 拒绝 downgrade；版本保持 0032、行保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0032")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, company_id)
        await _seed_link(temp_url, synthesis_id, claim_id)
        result_id = await _seed_result(temp_url, synthesis_id)
        outline_id = await _seed_outline(temp_url, result_id, company_id)
        assert await _table_count(temp_url, _OUTLINES) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0032"):
            await asyncio.to_thread(command.downgrade, cfg, "0031")

        assert await _version(temp_url) == "0032"
        assert await _table_count(temp_url, _OUTLINES) == 1
        # outline 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT outline_schema_version FROM report_outlines "
                            "WHERE outline_id = :oid"
                        ).bindparams(oid=outline_id)
                    )
                ).scalar_one()
                assert int(row) == 1
                results = int(
                    (
                        await session.execute(text("SELECT count(*) FROM claim_synthesis_results"))
                    ).scalar_one()
                )
                assert results == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
