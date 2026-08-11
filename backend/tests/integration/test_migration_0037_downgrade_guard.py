"""Integration test: migration 0037 draft_section_revisions downgrade guard (stage 5E.2A).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0037：

- (B) **空表通过**：无数据 → `alembic downgrade 0037 -> 0036` 成功、版本回到
  0036、`draft_section_revisions` 表被删；
- (A) **非空拒绝**：`draft_section_revisions` 存在行 → `alembic downgrade
  0037 -> 0036` 必须拒绝（RuntimeError），`alembic_version` 仍为 0037；revision /
  source draft / revised draft 的行数据完整保留（Revision 是正式 immutable
  research artifact——记录了裁决后的正文修订，链上已存在修订正文，不在 downgrade
  时静默删除历史）。

company / evidence / claim / synthesis run / input link / result / outline /
report / check_result / audit / review_issue / review action / human request /
human decision / draft_sections 用既有 guard 测试 helpers 直接 SQL seed；
`draft_section_revisions` 行直接 SQL 插入（满足全部 CHECK：revision_round >= 1、
trigger_type 合法、三个 trigger FK 恰好一个非空、source != revised、
revision_fingerprint 64 hex）。

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
from tests.integration.test_migration_0033_downgrade_guard import _seed_draft
from tests.integration.test_migration_0034_downgrade_guard import (
    _seed_check_result,
    _seed_report,
)
from tests.integration.test_migration_0035_downgrade_guard import _seed_audit, _seed_issue
from tests.integration.test_migration_0036_downgrade_guard import (
    _seed_action,
    _seed_decision,
    _seed_request,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_TABLE = "draft_section_revisions"


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


async def _seed_revision(
    temp_url: str,
    source_draft_id: UUID,
    revised_draft_id: UUID,
    *,
    check_result_id: UUID | None = None,
    action_id: UUID | None = None,
    decision_id: UUID | None = None,
) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 draft_section_revisions 行。

    三个 trigger FK 恰好一个非空：默认 check_result_id；也可指定 review action 或
    human decision（trigger_type 相应变化）。
    """
    revision_id = uuid4()
    if check_result_id is not None:
        trigger_type = "deterministic_check"
    elif action_id is not None:
        trigger_type = "audit_rewrite"
    else:
        trigger_type = "human_rewrite"
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO draft_section_revisions "
                    "(revision_id, source_draft_section_id, revised_draft_section_id, "
                    " revision_round, trigger_type, review_action_id, check_result_id, "
                    " human_decision_id, revision_schema_version, revision_fingerprint) "
                    "VALUES (CAST(:rid AS uuid), CAST(:sid AS uuid), CAST(:vdid AS uuid), "
                    " 1, :trigger_type, CAST(:aid AS uuid), CAST(:cid AS uuid), "
                    " CAST(:did AS uuid), 1, :fp)"
                ).bindparams(
                    rid=revision_id,
                    sid=source_draft_id,
                    vdid=revised_draft_id,
                    trigger_type=trigger_type,
                    aid=action_id,
                    cid=check_result_id,
                    did=decision_id,
                    fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return revision_id


@pytest.mark.asyncio
async def test_migration_0037_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0037 upgrade 建表；无数据 → downgrade 0037→0036 成功，表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0037")
        assert await _version(temp_url) == "0037"
        assert await _table_count(temp_url, _TABLE) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0036")
        assert await _version(temp_url) == "0036"
        assert await _table_exists(temp_url, _TABLE) is False
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0037_downgrade_blocked_with_revision(monkeypatch, tmp_path) -> None:
    """(A) draft_section_revisions 存在行 → 拒绝 downgrade；数据保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0037")

        # 完整上游链：company → claim → synthesis → outline → draft → report →
        # check → audit → issue → action → request → decision。
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, company_id)
        await _seed_link(temp_url, synthesis_id, claim_id)
        result_id = await _seed_result(temp_url, synthesis_id)
        outline_id = await _seed_outline(temp_url, result_id, company_id)
        source_draft_id = await _seed_draft(temp_url, outline_id, section_id="S1", section_order=1)
        revised_draft_id = await _seed_draft(
            temp_url, outline_id, section_id="S1", section_order=1, title="修订后标题"
        )
        report_id = await _seed_report(temp_url, outline_id, company_id)
        check_id = await _seed_check_result(temp_url, report_id)
        audit_id = await _seed_audit(temp_url, report_id, check_id)
        issue_id = await _seed_issue(temp_url, audit_id)
        action_id = await _seed_action(temp_url, audit_id, report_id, issue_id)
        request_id = await _seed_request(temp_url, action_id, audit_id, report_id)
        decision_id = await _seed_decision(temp_url, request_id)

        # 用 human_rewrite trigger（decision）插入 revision 行。
        revision_id = await _seed_revision(
            temp_url,
            source_draft_id,
            revised_draft_id,
            decision_id=decision_id,
        )
        assert await _table_count(temp_url, _TABLE) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0037"):
            await asyncio.to_thread(command.downgrade, cfg, "0036")

        assert await _version(temp_url) == "0037"
        assert await _table_count(temp_url, _TABLE) == 1
        # Revision / source draft / revised draft / 上游 chain 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                revision = (
                    await session.execute(
                        text(
                            "SELECT trigger_type, revision_round "
                            "FROM draft_section_revisions WHERE revision_id = :rid"
                        ).bindparams(rid=revision_id)
                    )
                ).one()
                assert revision[0] == "human_rewrite"
                assert revision[1] == 1
                drafts = int(
                    (
                        await session.execute(text("SELECT count(*) FROM draft_sections"))
                    ).scalar_one()
                )
                assert drafts == 2
                actions = int(
                    (
                        await session.execute(text("SELECT count(*) FROM report_review_actions"))
                    ).scalar_one()
                )
                assert actions == 1
                audits = int(
                    (await session.execute(text("SELECT count(*) FROM report_audits"))).scalar_one()
                )
                assert audits == 1
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0037_revision_schema_constraints_enforced(monkeypatch, tmp_path) -> None:
    """CHECK 约束生效：恰一个 trigger 非空、source != revised、round >= 1。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0037")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, company_id)
        await _seed_link(temp_url, synthesis_id, claim_id)
        result_id = await _seed_result(temp_url, synthesis_id)
        outline_id = await _seed_outline(temp_url, result_id, company_id)
        source_draft_id = await _seed_draft(temp_url, outline_id, section_id="S1", section_order=1)
        revised_draft_id = await _seed_draft(
            temp_url, outline_id, section_id="S1", section_order=1, title="修订后标题"
        )
        check_id = await _seed_check_result(
            temp_url, await _seed_report(temp_url, outline_id, company_id)
        )

        # 0 个 trigger → 拒绝（exactly one 约束）。
        with pytest.raises(Exception, match="exactly_one_trigger"):
            await _seed_revision(temp_url, source_draft_id, revised_draft_id)

        # source == revised → 拒绝（source != revised 约束）。
        with pytest.raises(Exception, match="source_ne_revised"):
            await _seed_revision(
                temp_url, source_draft_id, source_draft_id, check_result_id=check_id
            )

        assert await _table_count(temp_url, _TABLE) == 0
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
