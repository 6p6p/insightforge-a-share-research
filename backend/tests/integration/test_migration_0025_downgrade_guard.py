"""Integration test: migration 0025 analysis_as_of column downgrade guard (Gate 0).

在**独立临时 PostgreSQL 数据库**中真实验证 0025：

- (A) 0025 upgrade 给 `macro_transmission_chains` 加 `analysis_as_of DATE NULL` +
  CHECK `(transmission_schema_version < 3 OR analysis_as_of IS NOT NULL)` + 普通
  INDEX `(company_id, analysis_as_of)`；CHECK 拒绝 v3 链缺 cutoff；无数据 →
  `alembic downgrade 0025 -> 0024` 成功、版本回到 0024、列 / CHECK / INDEX 被删；
- (B) 拒绝 downgrade 的场景（各单测隔离一种触发条件）：
  - v3 transmission（transmission_schema_version >= 3）→ 拒绝；
  - v6 macro Claim（analysis_domain='macro' AND claim_schema_version >= 6）→ 拒绝；
  拒绝后 alembic_version 仍为 0025，数据完整保留（不删除 / 不改写 / 不丢弃
  analysis_as_of provenance）；
- (C) 只有 safe legacy v2/v5（claim v5 + transmission v2，cutoff NULL 合法）时
  downgrade 0025→0024 成功，列 / CHECK / INDEX 移除，历史对象原样保留。

company + document Claim 用真实服务链 seed（`_seed_document_claim`）；macro
Claim / transmission chain 用直接 SQL 插入（fingerprint 用生产函数
`compute_macro_*_fingerprint` 生成；v3 链必须带 analysis_as_of 满足 CHECK）。
测试全程使用 `insightforge_gate_*` 临时库并最终 DROP，不触碰主库
（`insightforge`）。需要真实 PostgreSQL（127.0.0.1:5433）且账号有 CREATEDB 权限。
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
from sqlalchemy.exc import IntegrityError

from alembic import command
from app.claims.contracts import compute_research_question_sha256
from app.claims.macro_contracts import (
    MACRO_CLAIM_SCHEMA_VERSION,
    MACRO_CLAIM_SCHEMA_VERSION_V5,
    MACRO_TRANSMISSION_SCHEMA_VERSION,
    MACRO_TRANSMISSION_SCHEMA_VERSION_V2,
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

_COLUMN_NAME = "analysis_as_of"
_CHECK_NAME = "ck_macro_transmission_chains_analysis_as_of_present"
_INDEX_NAME = "ix_macro_transmission_chains_company_analysis_as_of"


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


async def _column_exists(temp_url: str, column_name: str) -> bool:
    manager = DatabaseManager(database_url=temp_url, echo=False, connect_timeout_seconds=5)
    try:
        sessionmaker = manager.session_factory()
        async with sessionmaker() as session:
            count = (
                await session.execute(
                    text(
                        "SELECT count(*) FROM information_schema.columns "
                        "WHERE table_schema='public' AND table_name='macro_transmission_chains' "
                        "AND column_name = :name"
                    ).bindparams(name=column_name)
                )
            ).scalar_one()
            return int(count) > 0
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
    analysis_as_of: date | None = _ANALYSIS_AS_OF,
    statement: str = _STATEMENT,
) -> dict:
    """在 0025 schema 下 seed 一条 macro Claim + 一条传导链（全部 CHECK 满足）。

    fingerprint 用生产函数生成；`analysis_as_of` 提供时写入新查询列（v3 链必须
    NOT NULL 以满足 CHECK）；v2 链传 None 保持 legacy NULL 语义。
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
            trans_fp = compute_macro_transmission_fingerprint(
                transmission_schema_version=transmission_schema_version,
                company_id=company_id,
                channel_type="financing",
                effect_direction="headwind",
                impact_status="plausible_impact",
                time_alignment="aligned",
                analysis_as_of=analysis_as_of or _ANALYSIS_AS_OF,
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
                    " transmission_schema_version, transmission_fingerprint, analysis_as_of) "
                    "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), "
                    " CAST(:company_id AS uuid), 'financing', 'headwind', "
                    " 'plausible_impact', 'aligned', :schema_version, :fp, CAST(:cutoff AS date))"
                ).bindparams(
                    tid=transmission_id,
                    cid=claim_id,
                    company_id=company_id,
                    schema_version=transmission_schema_version,
                    fp=trans_fp,
                    cutoff=analysis_as_of,
                )
            )
            await session.commit()
        return {
            "claim_id": str(claim_id),
            "transmission_id": str(transmission_id),
        }
    finally:
        await manager.dispose()


@pytest.mark.asyncio
async def test_migration_0025_downgrade_allowed_when_empty(monkeypatch) -> None:
    """(A) 0025 无数据 → downgrade 0025→0024 成功：列 / CHECK / INDEX 被删。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0025")
        assert await _version(temp_url) == "0025"
        # upgrade：列 / CHECK / INDEX 全部建立。
        assert await _column_exists(temp_url, _COLUMN_NAME)
        assert await _constraint_exists(temp_url, _CHECK_NAME)
        assert await _index_exists(temp_url, _INDEX_NAME)

        await asyncio.to_thread(command.downgrade, cfg, "0024")
        assert await _version(temp_url) == "0024"
        assert not await _column_exists(temp_url, _COLUMN_NAME)
        assert not await _constraint_exists(temp_url, _CHECK_NAME)
        assert not await _index_exists(temp_url, _INDEX_NAME)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0025_check_blocks_v3_chain_without_cutoff(monkeypatch, tmp_path) -> None:
    """(A) CHECK 强制：v3 链缺 analysis_as_of → INSERT 拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0025")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")

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
                trans_fp = compute_macro_transmission_fingerprint(
                    transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
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
                with pytest.raises(IntegrityError):
                    await session.execute(
                        text(
                            "INSERT INTO macro_transmission_chains "
                            "(transmission_id, claim_id, company_id, channel_type, "
                            " effect_direction, impact_status, time_alignment, "
                            " transmission_schema_version, transmission_fingerprint, "
                            " analysis_as_of) "
                            "VALUES (CAST(:tid AS uuid), CAST(:cid AS uuid), "
                            " CAST(:company_id AS uuid), 'financing', 'headwind', "
                            " 'plausible_impact', 'aligned', :schema_version, :fp, NULL)"
                        ).bindparams(
                            tid=uuid4(),
                            cid=UUID(seeded["claim_id"]),
                            company_id=company_id,
                            schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,
                            fp=trans_fp,
                        )
                    )
                await session.rollback()
                # CHECK 拒绝后链表仍为空。
                count = (
                    await session.execute(text("SELECT count(*) FROM macro_transmission_chains"))
                ).scalar_one()
                assert int(count) == 0
        finally:
            await manager.dispose()
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0025_downgrade_blocked_with_v3_transmission(monkeypatch, tmp_path) -> None:
    """(B) v3 transmission（transmission_schema_version=3）→ downgrade 拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0025")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V5,  # 不触发 v6 条件
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION,  # =3
            analysis_as_of=_ANALYSIS_AS_OF,
        )
        assert await _chain_count(temp_url) == 1

        with pytest.raises(RuntimeError, match="v3 transmission"):
            await asyncio.to_thread(command.downgrade, cfg, "0024")

        # 拒绝后：版本仍为 0025、链数据完整保留（含 cutoff provenance）。
        assert await _version(temp_url) == "0025"
        assert await _chain_count(temp_url) == 1
        assert await _column_exists(temp_url, _COLUMN_NAME)
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0025_downgrade_blocked_with_v6_macro_claim(monkeypatch, tmp_path) -> None:
    """(B) v6 macro Claim（analysis_domain='macro' AND schema=6）→ downgrade 拒绝。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0025")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION,  # =6
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V2,  # 不触发 v3
            analysis_as_of=None,  # v2 链 NULL 合法
        )
        assert await _chain_count(temp_url) == 1

        with pytest.raises(RuntimeError, match="v6 macro claim"):
            await asyncio.to_thread(command.downgrade, cfg, "0024")

        assert await _version(temp_url) == "0025"
        assert await _chain_count(temp_url) == 1
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()


@pytest.mark.asyncio
async def test_migration_0025_downgrade_allowed_with_safe_legacy_data(
    monkeypatch, tmp_path
) -> None:
    """(C) 只有 safe legacy v2/v5（单条链、cutoff NULL）→ downgrade 成功。"""
    base_url = get_settings().database_url
    temp_db = f"insightforge_gate_{uuid4().hex[:12]}"
    temp_url = _temp_url(base_url, temp_db)

    _create_temp_db(temp_db)
    monkeypatch.setenv("DATABASE_URL", temp_url)
    get_settings.cache_clear()
    try:
        cfg = Config(str(ALEMBIC_INI))
        await asyncio.to_thread(command.upgrade, cfg, "0025")
        seeded = await _seed_document_claim(temp_url, tmp_path / "raw")
        macro = await _seed_macro_claim_chain(
            temp_url,
            seeded,
            claim_schema_version=MACRO_CLAIM_SCHEMA_VERSION_V5,
            transmission_schema_version=MACRO_TRANSMISSION_SCHEMA_VERSION_V2,
            analysis_as_of=None,
        )
        assert await _chain_count(temp_url) == 1

        await asyncio.to_thread(command.downgrade, cfg, "0024")
        assert await _version(temp_url) == "0024"
        # 列 / CHECK / INDEX 移除。
        assert not await _column_exists(temp_url, _COLUMN_NAME)
        assert not await _constraint_exists(temp_url, _CHECK_NAME)
        assert not await _index_exists(temp_url, _INDEX_NAME)
        # 历史 v2/v5 对象原样保留。
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
        assert chain == MACRO_TRANSMISSION_SCHEMA_VERSION_V2
        assert claim == MACRO_CLAIM_SCHEMA_VERSION_V5
    finally:
        try:
            _drop_temp_db(temp_db)
        finally:
            get_settings.cache_clear()
