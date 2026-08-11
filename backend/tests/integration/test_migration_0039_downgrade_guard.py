"""Integration test: migration 0039 report_exports downgrade guard (stage 6C).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0039：

- (B) **空表通过**：无数据 → `alembic downgrade 0039 -> 0038` 成功、版本回到
  0038、`report_exports` 表被删；随后 `upgrade 0039` 恢复表与版本（spec B：
  「完成后再 upgrade 回 0039」）。
- (A) **非空拒绝**：`report_exports` 存在行 → `alembic downgrade 0039 -> 0038`
  必须拒绝（RuntimeError），`alembic_version` 仍为 0039，export 行数据完整保留
  （Export 是正式 immutable research artifact——即使可确定性重放，也不在
  downgrade 时静默删除历史；stored bytes 由 storage_key 描述，行保留即归档
  描述不丢）。

company / evidence / claim / synthesis run / input link / result / outline /
report / check / audit / issue / research task / workflow run 复用既有 guard
测试 helpers 直接 SQL seed；`report_exports` 行直接 SQL 插入（满足全部 CHECK：
fingerprints / sha256 用 64 hex，byte_size > 0，format 合法）。

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
from tests.integration.test_migration_0035_downgrade_guard import (
    _seed_audit,
    _seed_issue,
)
from tests.integration.test_migration_0038_downgrade_guard import (
    _seed_research_task,
    _seed_workflow_run,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_EXPORTS = "report_exports"


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


async def _seed_report_export(
    temp_url: str,
    *,
    task_id: UUID,
    report_id: UUID,
    check_id: UUID,
    audit_id: UUID,
) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 report_exports 行（audit pass 路径）。

    human_decision_id 保持 NULL（spec H audit pass 路径）；fingerprint /
    content_sha256 各用唯一 64 hex；byte_size>0；format='markdown'。
    """
    export_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO report_exports "
                    "(export_id, task_id, report_id, check_result_id, audit_id, "
                    " human_decision_id, export_schema_version, export_format, "
                    " export_input_fingerprint, content_sha256, byte_size, "
                    " media_type, file_name, storage_key) "
                    "VALUES (CAST(:eid AS uuid), CAST(:tid AS uuid), "
                    " CAST(:rid AS uuid), CAST(:cid AS uuid), CAST(:aid AS uuid), "
                    " NULL, 1, 'markdown', :fp, :sha, 123, "
                    " 'text/markdown; charset=utf-8', 'report.md', :key)"
                ).bindparams(
                    eid=export_id,
                    tid=task_id,
                    rid=report_id,
                    cid=check_id,
                    aid=audit_id,
                    fp=_hex64(),
                    sha=_hex64(),
                    key=f"exports/sha256/{_hex64()}",
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return export_id


async def _seed_full_export_chain(temp_url: str, raw_root: Path) -> dict:
    """seed 整条 Source→…→Audit→ResearchTask→WorkflowRun→ReportExport 链。

    返回 {company_id, task_id, report_id, check_id, audit_id, export_id}。
    """
    chain = await _seed_chain(temp_url, raw_root / "raw", "600519")
    company_id = UUID(chain["company_id"])
    claim_id = await _seed_claim(temp_url, chain)
    synthesis_id = await _seed_run(temp_url, company_id)
    await _seed_link(temp_url, synthesis_id, claim_id)
    result_id = await _seed_result(temp_url, synthesis_id)
    outline_id = await _seed_outline(temp_url, result_id, company_id)
    report_id = await _seed_report(temp_url, outline_id, company_id)
    check_id = await _seed_check_result(temp_url, report_id)
    audit_id = await _seed_audit(temp_url, report_id, check_id)
    await _seed_issue(temp_url, audit_id)

    task_id = await _seed_research_task(temp_url)
    await _seed_workflow_run(temp_url, task_id)
    export_id = await _seed_report_export(
        temp_url,
        task_id=task_id,
        report_id=report_id,
        check_id=check_id,
        audit_id=audit_id,
    )
    return {
        "company_id": company_id,
        "task_id": task_id,
        "report_id": report_id,
        "check_id": check_id,
        "audit_id": audit_id,
        "export_id": export_id,
    }


@pytest.mark.asyncio
async def test_migration_0039_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0039 upgrade 建表；无数据 → downgrade 0039→0038 成功，表被删；再 upgrade 回 0039。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0039")
        assert await _version(temp_url) == "0039"
        assert await _table_exists(temp_url, _EXPORTS) is True
        assert await _table_count(temp_url, _EXPORTS) == 0

        # 空表 → downgrade 0039→0038 成功。
        await asyncio.to_thread(command.downgrade, cfg, "0038")
        assert await _version(temp_url) == "0038"
        assert await _table_exists(temp_url, _EXPORTS) is False

        # 完成后 upgrade 回 0039（spec B）。
        await asyncio.to_thread(command.upgrade, cfg, "0039")
        assert await _version(temp_url) == "0039"
        assert await _table_exists(temp_url, _EXPORTS) is True
        assert await _table_count(temp_url, _EXPORTS) == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0039_downgrade_blocked_with_export(monkeypatch, tmp_path) -> None:
    """(A) report_exports 存在行 → 拒绝 downgrade；行数据与版本完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0039")
        seeded = await _seed_full_export_chain(temp_url, tmp_path)
        assert await _table_count(temp_url, _EXPORTS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0039"):
            await asyncio.to_thread(command.downgrade, cfg, "0038")

        # alembic_version 仍 0039，表与行保留。
        assert await _version(temp_url) == "0039"
        assert await _table_exists(temp_url, _EXPORTS) is True
        assert await _table_count(temp_url, _EXPORTS) == 1

        # export 行数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT task_id, report_id, check_result_id, audit_id, "
                            "       export_format, byte_size, storage_key "
                            "FROM report_exports WHERE export_id = :eid"
                        ).bindparams(eid=seeded["export_id"])
                    )
                ).one()
                assert row[0] == seeded["task_id"]
                assert row[1] == seeded["report_id"]
                assert row[2] == seeded["check_id"]
                assert row[3] == seeded["audit_id"]
                assert row[4] == "markdown"
                assert row[5] > 0
                assert row[6].startswith("exports/sha256/")
                # 上游 artifact 数据保留。
                audits = int(
                    (await session.execute(text("SELECT count(*) FROM report_audits"))).scalar_one()
                )
                assert audits == 1
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
