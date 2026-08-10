"""Integration test: migration 0033 draft sections downgrade guard (stage 5B).

在**独立临时 PostgreSQL 数据库**（`insightforge_gate_*`）中真实验证 0033：

- (A) **非空拒绝**：`draft_sections` 存在行 → `alembic downgrade 0033 -> 0032`
  必须拒绝（RuntimeError），`alembic_version` 仍为 0033，行数据完整保留；
- (B) **空表通过**：无数据 → `alembic downgrade 0033 -> 0032` 成功、版本回到
  0032、表被删。

DraftSection 是正式 immutable research artifact，即使可确定性重放，也不在
downgrade 时静默删除历史（spec D：downgrade 语义不接受 simple drop）。

company / evidence / claim / synthesis run / input link / result / outline 用
0032 guard 测试 helpers 直接 SQL seed；draft_section 行直接 SQL 插入（满足
全部 CHECK：fingerprints 用 64 hex，payload 为合法 JSONB）。

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
from tests.integration.test_migration_0026_downgrade_guard import (
    _seed_chain,
)
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

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_DRAFTS = "draft_sections"


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


async def _seed_draft(
    temp_url: str,
    outline_id: UUID,
    *,
    section_id: str = "S1",
    section_order: int = 1,
    section_type: str = "theme",
    title: str = "多维度证据支持",
) -> UUID:
    """直接 SQL 插入一条满足全部 CHECK 的 draft_sections 行。"""
    draft_id = uuid4()
    payload = {
        "paragraphs": [
            {
                "text": "公司营收保持增长态势。",
                "claim_ids": [],
                "evidence_card_ids": [],
                "conflict_indexes": [],
                "evidence_gap_indexes": [],
            }
        ]
    }
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            await session.execute(
                text(
                    "INSERT INTO draft_sections "
                    "(draft_section_id, outline_id, section_id, section_order, "
                    " section_type, title, section_schema_version, writer_name, "
                    " writer_version, writer_model_id, writer_input_fingerprint, "
                    " section_payload, section_fingerprint) "
                    "VALUES (CAST(:did AS uuid), CAST(:oid AS uuid), :section_id, "
                    " :section_order, :section_type, :title, 1, "
                    " 'evidence_bound_section_writer', 1, 'deepseek:deepseek-v4-flash', "
                    " :input_fp, CAST(:payload AS jsonb), :section_fp)"
                ).bindparams(
                    did=draft_id,
                    oid=outline_id,
                    section_id=section_id,
                    section_order=section_order,
                    section_type=section_type,
                    title=title,
                    input_fp=_hex64(),
                    payload=json.dumps(payload, ensure_ascii=False),
                    section_fp=_hex64(),
                )
            )
            await session.commit()
    finally:
        await manager.dispose()
    return draft_id


@pytest.mark.asyncio
async def test_migration_0033_upgrade_and_downgrade_when_empty(monkeypatch) -> None:
    """(B) 0033 upgrade 建表；无数据 → downgrade 0033→0032 成功，表被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0033")
        assert await _version(temp_url) == "0033"
        assert await _table_count(temp_url, _DRAFTS) == 0

        await asyncio.to_thread(command.downgrade, cfg, "0032")
        assert await _version(temp_url) == "0032"
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                still = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM information_schema.tables "
                            "WHERE table_schema='public' AND table_name = :name"
                        ).bindparams(name=_DRAFTS)
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
async def test_migration_0033_downgrade_blocked_with_draft(monkeypatch, tmp_path) -> None:
    """(A) draft_sections 存在行 → 拒绝 downgrade；版本保持 0033、行保留。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0033")
        chain = await _seed_chain(temp_url, tmp_path / "raw", "600519")
        company_id = UUID(chain["company_id"])
        claim_id = await _seed_claim(temp_url, chain)
        synthesis_id = await _seed_run(temp_url, company_id)
        await _seed_link(temp_url, synthesis_id, claim_id)
        result_id = await _seed_result(temp_url, synthesis_id)
        outline_id = await _seed_outline(temp_url, result_id, company_id)
        draft_id = await _seed_draft(temp_url, outline_id)
        assert await _table_count(temp_url, _DRAFTS) == 1

        with pytest.raises(RuntimeError, match="cannot downgrade migration 0033"):
            await asyncio.to_thread(command.downgrade, cfg, "0032")

        assert await _version(temp_url) == "0033"
        assert await _table_count(temp_url, _DRAFTS) == 1
        # draft 数据未被删除 / 改写。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                section_version = (
                    await session.execute(
                        text(
                            "SELECT section_schema_version FROM draft_sections "
                            "WHERE draft_section_id = :did"
                        ).bindparams(did=draft_id)
                    )
                ).scalar_one()
                assert int(section_version) == 1
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
