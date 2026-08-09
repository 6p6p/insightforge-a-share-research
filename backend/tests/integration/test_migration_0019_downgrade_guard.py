"""Integration test: migration 0019 cross-relation uniqueness (stage 4A closeout).

在**独立临时 PostgreSQL 数据库**中真实验证 0019：

- (A) `alembic upgrade 0019` 新增 `UNIQUE(claim_id, evidence_card_id)`；
  无数据 → downgrade 0019→0018 成功、约束被移除、版本回到 0018；
- (B) 存在 ClaimEvidenceLink 数据时 downgrade 0019→0018 必须被拒绝
  （RuntimeError），且 alembic_version 仍为 0019；
- (C) **Gate 0A 核心验收**：真实 PostgreSQL 上，同 claim A + evidence E
  已有 supports 行后，直接 SQL 插入 claim A + evidence E + contradicts
  必须被数据库 UNIQUE 拒绝（不再只依赖应用层 ClaimDraft 约束）。

(B)/(C) 的 Claim 用**真实服务链** seed：SourceRecord → SourceParsingService →
ChunkingService → EvidenceCardService.create_card → ClaimService.create_claim
（零 Chroma / 零 LLM）。测试全程使用 `insightforge_gate_*` 临时库并最终
DROP，不触碰主库（`insightforge`）。
"""

import asyncio
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0018_downgrade_guard import (
    _seed_document_claim,
)

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_CONSTRAINT_NAME = "uq_claim_evidence_links_claim_evidence"


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


async def _has_constraint(temp_url: str, constraint: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            count = (
                await session.execute(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :name").bindparams(
                        name=constraint
                    )
                )
            ).scalar_one()
            return int(count) > 0
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0019_downgrade_allowed_when_empty(monkeypatch) -> None:
    """(A) 0019 无数据 → downgrade 0019→0018 成功，约束被移除、版本回到 0018。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0019")
        assert await _version(temp_url) == "0019"
        assert await _has_constraint(temp_url, _CONSTRAINT_NAME)

        await asyncio.to_thread(command.downgrade, cfg, "0018")
        assert await _version(temp_url) == "0018"
        assert not await _has_constraint(temp_url, _CONSTRAINT_NAME)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0019_downgrade_blocked_with_link_data(monkeypatch, tmp_path) -> None:
    """(B) 存在 ClaimEvidenceLink 数据时 downgrade 0019→0018 必须被拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0019")
        await _seed_document_claim(temp_url, tmp_path / "raw")

        with pytest.raises(RuntimeError, match="claim_evidence_links rows present"):
            await asyncio.to_thread(command.downgrade, cfg, "0018")

        # guard 拒绝后：版本仍为 0019、约束仍存在。
        assert await _version(temp_url) == "0019"
        assert await _has_constraint(temp_url, _CONSTRAINT_NAME)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_cross_relation_duplicate_rejected_by_database(monkeypatch, tmp_path) -> None:
    """(C) Gate 0A 核心：同 claim + 同 evidence 的跨 relation 重复由数据库拒绝。

    已存在 supports 行（经真实服务链 seed），直接 SQL 插入同 claim + 同
    evidence 的 contradicts 必须被 UNIQUE(claim_id, evidence_card_id) 拒绝，
    不能只依赖应用层 ClaimDraft 校验。
    """
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0019")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        claim_id = seeded["claim_id"]
        evidence_card_id = seeded["evidence_card_id"]

        # 已有的 supports 行。
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                supports_rows = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM claim_evidence_links "
                            "WHERE claim_id = CAST(:c AS uuid) "
                            "AND evidence_card_id = CAST(:e AS uuid) "
                            "AND relation = 'supports'"
                        ).bindparams(c=claim_id, e=evidence_card_id)
                    )
                ).scalar_one()
                assert int(supports_rows) == 1

                # 直接 SQL 插入 contradicts：必须被数据库 UNIQUE 拒绝。
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "INSERT INTO claim_evidence_links "
                            "(claim_id, evidence_card_id, relation) "
                            "VALUES (CAST(:c AS uuid), CAST(:e AS uuid), 'contradicts')"
                        ).bindparams(c=claim_id, e=evidence_card_id)
                    )
                await session.rollback()

                # 拒绝后无残留 contradicts 行，原 supports 行完整保留。
                contradicts_rows = (
                    await session.execute(
                        text(
                            "SELECT count(*) FROM claim_evidence_links "
                            "WHERE claim_id = CAST(:c AS uuid) "
                            "AND evidence_card_id = CAST(:e AS uuid) "
                            "AND relation = 'contradicts'"
                        ).bindparams(c=claim_id, e=evidence_card_id)
                    )
                ).scalar_one()
                assert int(contradicts_rows) == 0
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
