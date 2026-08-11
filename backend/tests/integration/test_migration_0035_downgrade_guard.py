"""Integration test: migration 0035 report audits + review issues downgrade guard (stage 5D).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0035：

- (B) **空表通过**：无数据 → `alembic downgrade 0035 -> 0034` 成功、版本回到
  0034、`report_audits` / `review_issues` 两表均被删；
- (A) **非空拒绝**：`report_audits` / `review_issues` 存在行 → `alembic
  downgrade 0035 -> 0034` 必须拒绝（RuntimeError），`alembic_version` 仍为
  0035；audit / issue / report / check 的行数据完整保留（Audit 是正式 immutable
  research artifact，即使可确定性重放，也不在 downgrade 时静默删除历史）。

company / evidence / claim / synthesis run / input link / result / outline 用既有
guard 测试 helpers 直接 SQL seed；report / check_result / audit / review_issue 行
直接 SQL 插入（满足全部 CHECK：fingerprints 用 64 hex，payload / findings / ids
为合法 JSONB，status / route / severity 合法）。

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
from tests.integration.test_migration_0027_downgrade_guard import _seed_claim
from tests.integration.test_migration_0028_downgrade_guard import (
    _hex64,
    _seed_link,
    _seed_run,
)
from tests.integration.test_migration_0032_downgrade_guard import (
    _seed_outline,
    _seed_result,
)
from tests.integration.test_migration_0034_downgrade_guard import (
    _seed_check_result,
    _seed_report,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_AUDITS = "report_audits"
_ISSUES = "review_issues"


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


async def _seed_audit(temp_url: str, report_id: UUID, check_result_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 report_audits 行。"""
    audit_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO report_audits "
                    "(audit_id, report_id, check_result_id, audit_schema_version, "
                    " auditor_name, auditor_version, auditor_model_id, "
                    " audit_input_fingerprint, audit_status, recommended_route, "
                    " issue_count, audit_fingerprint) "
                    "VALUES (CAST(:aid AS uuid), CAST(:rid AS uuid), "
                    " CAST(:cid AS uuid), 1, "
                    " 'evidence_bound_report_auditor', 1, "
                    " 'deepseek:deepseek-v4-flash', :input_fp, 'fail', 'rewrite', "
                    " 1, :audit_fp)"
                ).bindparams(
                    aid=audit_id,
                    rid=report_id,
                    cid=check_result_id,
                    input_fp=_hex64(),
                    audit_fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return audit_id


async def _seed_issue(temp_url: str, audit_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 review_issues 行。"""
    issue_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO review_issues "
                    "(review_issue_id, audit_id, ordinal, issue_type, severity, "
                    " section_id, paragraph_index, message, "
                    " related_claim_ids, related_evidence_card_ids) "
                    "VALUES (CAST(:iid AS uuid), CAST(:aid AS uuid), 1, "
                    " 'evidence_mismatch', 'high', 'S1', 0, '测试 issue', "
                    " CAST('[]' AS jsonb), CAST('[]' AS jsonb))"
                ).bindparams(iid=issue_id, aid=audit_id)
            )
            await session.commit()
    finally:
        await manager.dispose()
    return issue_id


@pytest.mark.asyncio
async def test_migration_0035_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0035 upgrade 建两表；无数据 → downgrade 0035→0034 成功，两表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0035")
        assert await _version(temp_url) == "0035"
        assert await _table_count(temp_url, _AUDITS) == 0
        assert await _table_count(temp_url, _ISSUES) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0034")
        assert await _version(temp_url) == "0034"
        assert await _table_exists(temp_url, _AUDITS) is False
        assert await _table_exists(temp_url, _ISSUES) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0035_downgrade_blocked_with_audit_and_issue(monkeypatch, tmp_path) -> None:
    """(A) report_audits + review_issues 存在行 → 拒绝 downgrade；数据保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0035")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, company_id)
        await _seed_link(temp_url, synthesis_id, claim_id)
        result_id = await _seed_result(temp_url, synthesis_id)
        outline_id = await _seed_outline(temp_url, result_id, company_id)
        report_id = await _seed_report(temp_url, outline_id, company_id)
        check_id = await _seed_check_result(temp_url, report_id)
        audit_id = await _seed_audit(temp_url, report_id, check_id)
        issue_id = await _seed_issue(temp_url, audit_id)
        assert await _table_count(temp_url, _AUDITS) == 1
        assert await _table_count(temp_url, _ISSUES) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0035"):
            await asyncio.to_thread(command.downgrade, cfg, "0034")

        assert await _version(temp_url) == "0035"
        assert await _table_count(temp_url, _AUDITS) == 1
        assert await _table_count(temp_url, _ISSUES) == 1
        # Audit / Issue / Report / Check 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                audit = (
                    await session.execute(
                        text(
                            "SELECT audit_status FROM report_audits WHERE audit_id = :aid"
                        ).bindparams(aid=audit_id)
                    )
                ).scalar_one()
                assert audit == "fail"
                issue = (
                    await session.execute(
                        text(
                            "SELECT issue_type FROM review_issues WHERE review_issue_id = :iid"
                        ).bindparams(iid=issue_id)
                    )
                ).scalar_one()
                assert issue == "evidence_mismatch"
                check = (
                    await session.execute(
                        text(
                            "SELECT status FROM report_check_results WHERE check_result_id = :cid"
                        ).bindparams(cid=check_id)
                    )
                ).scalar_one()
                assert check == "pass"
                reports = int(
                    (await session.execute(text("SELECT count(*) FROM reports"))).scalar_one()
                )
                assert reports == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
