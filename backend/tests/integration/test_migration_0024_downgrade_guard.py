"""Integration test: migration 0024 transmission fingerprint ownership guard.

在**独立临时 PostgreSQL 数据库**中真实验证 0024：

- (A) 0024 upgrade 把 `macro_transmission_chains.transmission_fingerprint` 的
  global UNIQUE 换成普通 INDEX；无数据 → `alembic downgrade 0024 -> 0023` 成功、
  版本回到 0023、INDEX 被删、UNIQUE 恢复；
- (B) 拒绝 downgrade 的场景（各单测隔离一种触发条件，guard 文案不同）：
  - v2 transmission（transmission_schema_version >= 2）→ 拒绝；
  - v5 macro Claim（analysis_domain='macro' AND claim_schema_version >= 5）→ 拒绝；
  - 重复 transmission_fingerprint（GROUP BY HAVING count>1）→ 拒绝；
  拒绝后 alembic_version 仍为 0024，数据完整保留（不删除 / 不改写 fingerprint /
  不静默合并链）；
- (C) 只有 safe legacy v1/v4（单条链、唯一 fingerprint）时 downgrade 0024→0023
  成功，UNIQUE 恢复，历史对象原样保留。

company + document Claim 用真实服务链 seed（`_seed_document_claim`）；macro
Claim / transmission chain 用直接 SQL 插入（fingerprint 用生产函数
`compute_macro_*_fingerprint` 生成）。测试全程使用 `insightforge_gate_*` 临时库
并最终 DROP，不触碰主库（`insightforge`）。需要真实 PostgreSQL
（127.0.0.1:5433）且账号有 CREATEDB 权限。
"""

import asyncio
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
from uuid import UUID, uuid4

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.claims.contracts import compute_research_question_sha256
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_CLAIM_SCHEMA_VERSION_V4,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION_V1,
    compute_macro_claim_fingerprint,
    compute_macro_transmission_fingerprint,
)
from app.core.config import get_settings
from app.db.session import DatabaseManager
from tests.integration.test_migration_0018_downgrade_guard import _seed_document_claim

pytestmark = pytest.mark.integration

BACKEND_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"

_QUESTION = "利率上行对贵州茅台融资成本的影响？"
_STATEMENT = "若利率持续上行，公司融资成本存在上升压力。"
_ANALYSIS_AS_OF = date(2026, 8, 10)

_UNIQUE_NAME = "uq_macro_transmission_chains_transmission_fingerprint"
_INDEX_NAME = "ix_macro_transmission_chains_transmission_fingerprint"


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


async def _constraint_exists(temp_url: str, constraint_name: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.table_constraints "
                        "WHERE table_schema='public' AND table_name='macro_transmission_chains' "
                        "AND constraint_name = :name"
                    ).bindparams(name=constraint_name)
                )
            ).scalar_one()
            return int(count) > 0
    finally:
        await manager.dispose()


async def _index_exists(temp_url: str, index_name: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM pg_indexes WHERE schemaname='public' "
                        "AND tablename='macro_transmission_chains' AND indexname = :name"
                    ).bindparams(name=index_name)
                )
            ).scalar_one()
            return int(count) > 0
    finally:
        await manager.dispose()


async def _chain_count(temp_url: str) -> int:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            return int(
                (
                    await session.execute(text("SELECT count(*) FROM macro_transmission_chains"))
                ).scalar_one()
            )
    finally:
        await manager.dispose()


async def _seed_macro_claim_chain(
    temp_url: str,
    seeded: dict,
    *,
    claim_schema_version: int,
    transmission_schema_version: int,
    transmission_fingerprint: str | None = None,
    statement: str = _STATEMENT,
) -> dict:
    """在 0024 schema 下 seed 一条 macro Claim + 一条传导链（全部 CHECK 满足）。

    fingerprint 用生产函数生成；`transmission_fingerprint` 提供时复用（构造重复
    fingerprint 场景），statement 可变化以错开 claim_fingerprint（claim_fingerprint
    UNIQUE）。
    """
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            company_id = (
                await session.execute(
                    text("SELECT company_id FROM claims WHERE claim_id = :cid").bindparams(
                        cid=UUID(seeded["claim_id"])
                    )
                )
            ).scalar_one()
            trans_fp = transmission_fingerprint or compute_macro_transmission_fingerprint(
                transmission_schema_version=transmission_schema_version,
                company_id=company_id,
                channel_type="financing",
                effect_direction="headwind",
                impact_status="plausible_impact",
                time_alignment="aligned",
                analysis_as_of=_ANALYSIS_AS_OF,
                macro_driver=[],
                company_exposure=[],
                observed_effect=[],
            )
            claim_fp = compute_macro_claim_fingerprint(
                claim_schema_version=claim_schema_version,
                company_id=company_id,
                research_question=_QUESTION,
                analysis_as_of=_ANALYSIS_AS_OF,
                statement=statement,
                claim_kind="risk",
                confidence="medium",
                importance="normal",
                analyst_name="macro-analyst",
                analyst_version=1,
                analyst_model_id="deepseek:deepseek-v4-flash",
                transmission_fingerprint=trans_fp,
                additional_supports=[],
                additional_contradicts=[],
                additional_context=[],
            )
            claim_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO claims "
                    "(claim_id, company_id, research_question, research_question_sha256, "
                    " statement, analysis_domain, claim_kind, confidence, importance, "
                    " analyst_name, analyst_version, analyst_model_id, "
                    " claim_schema_version, claim_fingerprint) "
                    "VALUES (CAST(:cid AS uuid), CAST(:company_id AS uuid), :rq, :rq_sha, "
                    " :statement, 'macro', 'risk', 'medium', 'normal', 'macro-analyst', "
                    " 1, :model_id, :schema_version, :fp)"
                ).bindparams(
                    cid=claim_id,
                    company_id=company_id,
                    rq=_QUESTION,
                    rq_sha=compute_research_question_sha256(_QUESTION),
                    statement=statement,
                    model_id="deepseek:deepseek-v4-flash",
                    schema_version=claim_schema_version,
                    fp=claim_fp,
                )
            )
            transmission_id = uuid4()
            await session.execute(
                text(
                    "INSERT INTO macro_transmission_chains "
                    "(transmission_id, claim_id, company_id, channel_type, "
                    " effect_direction, impact_status, time_alignment, "
                    " transmission_schema_version, transmission_fingerprint) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), "
                    " CAST(:company_id AS uuid), 'financing', 'headwind', "
                    " 'plausible_impact', 'aligned', :schema_version, :fp)"
                ).bindparams(
                    tid=transmission_id,
                    cid=claim_id,
                    company_id=company_id,
                    schema_version=transmission_schema_version,
                    fp=trans_fp,
                )
            )
            await session.commit()
        return {
            "claim_id": str(claim_id),
            "transmission_id": str(transmission_id),
            "transmission_fingerprint": trans_fp,
        }
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0024_downgrade_allowed_when_empty(monkeypatch) -> None:
    """(A) 0024 无数据 → downgrade 0024→0023 成功：INDEX 被删、UNIQUE 恢复。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0024")
        assert await _version(temp_url) == "0024"
        # upgrade：UNIQUE 已移除，普通 INDEX 建立。
        assert not await _constraint_exists(temp_url, _UNIQUE_NAME)
        assert await _index_exists(temp_url, _INDEX_NAME)

        await asyncio.to_thread(command.downgrade, cfg, "0023")
        assert await _version(temp_url) == "0023"
        assert not await _index_exists(temp_url, _INDEX_NAME)
        assert await _constraint_exists(temp_url, _UNIQUE_NAME)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0024_downgrade_blocked_with_v2_transmission(monkeypatch, tmp_path) -> None:
    """(B) v2 transmission（transmission_schema_version=2）→ downgrade 拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0024")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V4,  # 不触发 v5 条件
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,  # =2
        )
        assert await _chain_count(temp_url) == 1

        with pytest.raises(RuntimeError, match="v2 transmission"):
            await asyncio.to_thread(command.downgrade, cfg, "0023")

        # 拒绝后：版本仍为 0024、链数据完整保留。
        assert await _version(temp_url) == "0024"
        assert await _chain_count(temp_url) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0024_downgrade_blocked_with_v5_macro_claim(monkeypatch, tmp_path) -> None:
    """(B) v5 macro Claim（analysis_domain='macro' AND schema=5）→ downgrade 拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0024")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION,  # =5
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V1,  # 不触发 v2
        )
        assert await _chain_count(temp_url) == 1

        with pytest.raises(RuntimeError, match="v5 macro claim"):
            await asyncio.to_thread(command.downgrade, cfg, "0023")

        assert await _version(temp_url) == "0024"
        assert await _chain_count(temp_url) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0024_downgrade_blocked_with_duplicate_fingerprint(
    monkeypatch, tmp_path
) -> None:
    """(B) 重复 transmission_fingerprint → downgrade 拒绝（不静默合并链）。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0024")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        # 第一条链产生真实 v1 transmission fingerprint；第二条链复用同一 fingerprint
        # （claim 不同 → claim_fingerprint 不同，claims.claim_fingerprint UNIQUE 不冲突；
        # 0024 移除 fingerprint UNIQUE 后允许同指纹多条链）。
        first = await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V4,
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V1,
        )
        await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V4,
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V1,
            transmission_fingerprint=first["transmission_fingerprint"],
            statement="另一条相同传导语义的融资成本压力观点。",
        )
        assert await _chain_count(temp_url) == 2

        with pytest.raises(RuntimeError, match="duplicate transmission_fingerprint"):
            await asyncio.to_thread(command.downgrade, cfg, "0023")

        assert await _version(temp_url) == "0024"
        assert await _chain_count(temp_url) == 2
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0024_downgrade_allowed_with_safe_legacy_data(
    monkeypatch, tmp_path
) -> None:
    """(C) 只有 safe legacy v1/v4（单条链、唯一 fingerprint）→ downgrade 成功。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0024")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        macro = await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V4,
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V1,
        )
        assert await _chain_count(temp_url) == 1

        await asyncio.to_thread(command.downgrade, cfg, "0023")
        assert await _version(temp_url) == "0023"
        # UNIQUE 恢复、普通 INDEX 移除。
        assert not await _index_exists(temp_url, _INDEX_NAME)
        assert await _constraint_exists(temp_url, _UNIQUE_NAME)
        # 历史 v1/v4 对象原样保留（fingerprint / claim_id UNIQUE 语义不受损）。
        assert await _chain_count(temp_url) == 1
        manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
        try:
            sessionmaker = manager.session_factory()
            async with sessionmaker() as session:
                claim_id = UUID(macro["claim_id"])
                chain = (
                    await session.execute(
                        text(
                            "SELECT transmission_schema_version FROM "
                            "macro_transmission_chains WHERE claim_id = :cid"
                        ).bindparams(cid=claim_id)
                    )
                ).scalar_one()
                claim = (
                    await session.execute(
                        text(
                            "SELECT claim_schema_version FROM claims WHERE claim_id = :cid"
                        ).bindparams(cid=claim_id)
                    )
                ).scalar_one()
        finally:
            await manager.dispose()
        assert chain == MACRO_TRANSMISSION_SCHEMA_VERSION_V1
        assert claim == MACRO_CLAIM_SCHEMA_VERSION_V4
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
