"""Migration 0036 review actions + human confirmation downgrade guard (stage 5E.1).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0036：

- (B) **空表通过**：无数据 → `alembic downgrade 0036 -> 0035` 成功、版本回到
  0035、`report_review_actions` / `human_review_requests` /
  `human_review_decisions` 三表均被删；
- (A) **非空拒绝**：任一表存在行 → `alembic downgrade 0036 -> 0035` 必须拒绝
  （RuntimeError），`alembic_version` 仍为 0036；action / request / decision /
  audit / issue / report / check 的行数据完整保留（ReviewAction / human request /
  decision 是正式 immutable research artifact，即使可确定性重放，也不在
  downgrade 时静默删除历史）。

company / evidence / claim / synthesis run / input link / result / outline 用既有
guard 测试 helpers 直接 SQL seed；report / check_result / audit / review_issue 行
直接 SQL 插入（满足全部 CHECK），action / request / decision 直接 SQL 插入
（action_type=human_review，fingerprints 用 64 hex，payload 为合法 JSONB）。

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

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_ACTIONS = "report_review_actions"
_REQUESTS = "human_review_requests"
_DECISIONS = "human_review_decisions"


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


async def _seed_action(temp_url: str, audit_id: UUID, report_id: UUID, issue_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 report_review_actions 行（human_review）。"""
    action_id = uuid4()
    payload = {
        "source_report_id": str(report_id),
        "source_audit_id": str(audit_id),
        "target_section_ids": ["S1"],
        "review_issue_ids": [str(issue_id)],
    }
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO report_review_actions "
                    "(review_action_id, audit_id, report_id, action_schema_version, "
                    " action_type, action_payload, action_fingerprint) "
                    "VALUES (CAST(:aid AS uuid), CAST(:auid AS uuid), "
                    " CAST(:rid AS uuid), 1, 'human_review', "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    aid=action_id,
                    auid=audit_id,
                    rid=report_id,
                    payload=json.dumps(payload, ensure_ascii=False),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return action_id


async def _seed_request(temp_url: str, action_id: UUID, audit_id: UUID, report_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 human_review_requests 行。"""
    request_id = uuid4()
    payload = {
        "report_id": str(report_id),
        "audit_id": str(audit_id),
        "review_issue_ids": [],
        "section_ids": ["S1"],
        "issue_summaries": [
            {
                "issue_type": "unresolved_conflict",
                "severity": "critical",
                "section_id": "S1",
                "paragraph_index": 0,
            }
        ],
    }
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO human_review_requests "
                    "(human_request_id, review_action_id, request_schema_version, "
                    " request_payload, request_fingerprint) "
                    "VALUES (CAST(:rid AS uuid), CAST(:aid AS uuid), 1, "
                    " CAST(:payload AS jsonb), :fp)"
                ).bindparams(
                    rid=request_id,
                    aid=action_id,
                    payload=json.dumps(payload, ensure_ascii=False),
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return request_id


async def _seed_decision(temp_url: str, request_id: UUID) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 human_review_decisions 行。"""
    decision_id = uuid4()
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO human_review_decisions "
                    "(human_decision_id, human_request_id, decision_schema_version, "
                    " decision, comment, decided_at, decision_fingerprint) "
                    "VALUES (CAST(:did AS uuid), CAST(:rid AS uuid), 1, "
                    " 'approve', NULL, now(), :fp)"
                ).bindparams(did=decision_id, rid=request_id, fp=_hex64())
            )
            await session.commit()
    finally:
        await manager.dispose()
    return decision_id


@pytest.mark.asyncio
async def test_migration_0036_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0036 upgrade 建三表；无数据 → downgrade 0036→0035 成功，三表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0036")
        assert await _version(temp_url) == "0036"
        assert await _table_count(temp_url, _ACTIONS) == 0
        assert await _table_count(temp_url, _REQUESTS) == 0
        assert await _table_count(temp_url, _DECISIONS) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0035")
        assert await _version(temp_url) == "0035"
        assert await _table_exists(temp_url, _ACTIONS) is False
        assert await _table_exists(temp_url, _REQUESTS) is False
        assert await _table_exists(temp_url, _DECISIONS) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0036_downgrade_blocked_with_actions_and_decisions(
    monkeypatch, tmp_path
) -> None:
    """(A) review 三表存在行 → 拒绝 downgrade；数据完整保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0036")
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
        action_id = await _seed_action(temp_url, audit_id, report_id, issue_id)
        request_id = await _seed_request(temp_url, action_id, audit_id, report_id)
        decision_id = await _seed_decision(temp_url, request_id)
        assert await _table_count(temp_url, _ACTIONS) == 1
        assert await _table_count(temp_url, _REQUESTS) == 1
        assert await _table_count(temp_url, _DECISIONS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0036"):
            await asyncio.to_thread(command.downgrade, cfg, "0035")

        assert await _version(temp_url) == "0036"
        assert await _table_count(temp_url, _ACTIONS) == 1
        assert await _table_count(temp_url, _REQUESTS) == 1
        assert await _table_count(temp_url, _DECISIONS) == 1
        # Review 层数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                action = (
                    await session.execute(
                        text(
                            "SELECT action_type FROM report_review_actions "
                            "WHERE review_action_id = :aid"
                        ).bindparams(aid=action_id)
                    )
                ).scalar_one()
                assert action == "human_review"
                decision = (
                    await session.execute(
                        text(
                            "SELECT decision FROM human_review_decisions "
                            "WHERE human_decision_id = :did"
                        ).bindparams(did=decision_id)
                    )
                ).scalar_one()
                assert decision == "approve"
                audit = (
                    await session.execute(
                        text(
                            "SELECT recommended_route FROM report_audits WHERE audit_id = :aid"
                        ).bindparams(aid=audit_id)
                    )
                ).scalar_one()
                assert audit == "rewrite"
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
