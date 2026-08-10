"""Integration test: migration 0034 reports + check results downgrade guard (stage 5C).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0034：

- (B) **空表通过**：无数据 → `alembic downgrade 0034 -> 0033` 成功、版本回到
  0033、`reports` / `report_check_results` 两表均被删；
- (A) **非空拒绝**：`reports` 存在行 → `alembic downgrade 0034 -> 0033` 必须拒绝
  （RuntimeError），`alembic_version` 仍为 0034；`reports` 与
  `report_check_results` 的行数据完整保留（Report / CheckResult 是正式 immutable
  research artifact，即使可确定性重放，也不在 downgrade 时静默删除历史）。

company / evidence / claim / synthesis run / input link / result / outline 用既有
guard 测试 helpers 直接 SQL seed；report / check_result 行直接 SQL 插入（满足全部
CHECK：fingerprints 用 64 hex，payload / findings 为合法 JSONB，status 合法）。

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
from tests.integration.test_migration_0032_downgrade_guard import (
    _seed_outline,
    _seed_result,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_REPORTS = "reports"
_CHECK_RESULTS = "report_check_results"


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


async def _seed_report(temp_url: str, outline_id: UUID, company_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 reports 行。"""
    report_id = uuid4()
    rq_sha = compute_research_question_sha256(_QUESTION)
    payload = {
        "sections": [
            {
                "section_id": "S1",
                "section_order": 1,
                "section_type": "theme",
                "title": "主题",
                "draft_section_id": str(uuid4()),
                "paragraphs": [
                    {
                        "text": "公司营收保持增长态势。",
                        "claim_ids": [],
                        "evidence_card_ids": [],
                        "conflict_indexes": [],
                        "evidence_gap_indexes": [],
                    }
                ],
            }
        ]
    }
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO reports "
                    "(report_id, outline_id, company_id, research_question_sha256, "
                    " analysis_as_of, report_schema_version, report_payload, "
                    " report_fingerprint) "
                    "VALUES (CAST(:rid AS uuid), CAST(:oid AS uuid), "
                    " CAST(:company_id AS uuid), :rq_sha, CAST(:asof AS date), 1, "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    rid=report_id,
                    oid=outline_id,
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
    return report_id


async def _seed_check_result(temp_url: str, report_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 report_check_results 行。"""
    check_result_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO report_check_results "
                    "(check_result_id, report_id, check_schema_version, status, "
                    " findings, check_fingerprint) "
                    "VALUES (CAST(:cid AS uuid), CAST(:rid AS uuid), 1, 'pass', "
                    " CAST(:findings AS jsonb), :fp)"
                ).bindparams(
                    cid=check_result_id,
                    rid=report_id,
                    findings=json.dumps([]),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return check_result_id


@pytest.mark.asyncio
async def test_migration_0034_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0034 upgrade 建两表；无数据 → downgrade 0034→0033 成功，两表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0034")
        assert await _version(temp_url) == "0034"
        assert await _table_count(temp_url, _REPORTS) == 0
        assert await _table_count(temp_url, _CHECK_RESULTS) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0033")
        assert await _version(temp_url) == "0033"
        assert await _table_exists(temp_url, _REPORTS) is False
        assert await _table_exists(temp_url, _CHECK_RESULTS) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0034_downgrade_blocked_with_report_and_check(
    monkeypatch, tmp_path
) -> None:
    """(A) reports + report_check_results 存在行 → 拒绝 downgrade；数据保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0034")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, company_id)
        await _seed_link(temp_url, synthesis_id, claim_id)
        result_id = await _seed_result(temp_url, synthesis_id)
        outline_id = await _seed_outline(temp_url, result_id, company_id)
        report_id = await _seed_report(temp_url, outline_id, company_id)
        check_id = await _seed_check_result(temp_url, report_id)
        assert await _table_count(temp_url, _REPORTS) == 1
        assert await _table_count(temp_url, _CHECK_RESULTS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0034"):
            await asyncio.to_thread(command.downgrade, cfg, "0033")

        assert await _version(temp_url) == "0034"
        assert await _table_count(temp_url, _REPORTS) == 1
        assert await _table_count(temp_url, _CHECK_RESULTS) == 1
        # Report / CheckResult 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                report = (
                    await session.execute(
                        text(
                            "SELECT report_schema_version FROM reports WHERE report_id = :rid"
                        ).bindparams(rid=report_id)
                    )
                ).scalar_one()
                assert int(report) == 1
                check = (
                    await session.execute(
                        text(
                            "SELECT status FROM report_check_results WHERE check_result_id = :cid"
                        ).bindparams(cid=check_id)
                    )
                ).scalar_one()
                assert check == "pass"
                outlines = int(
                    (
                        await session.execute(text("SELECT count(*) FROM report_outlines"))
                    ).scalar_one()
                )
                assert outlines == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
